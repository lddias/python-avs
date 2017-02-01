import io
import logging
import sched
import ujson as json
import uuid

from h2.exceptions import StreamClosedError
from requests_toolbelt import MultipartEncoder
from requests_toolbelt.multipart.encoder import total_len

import audio_player
import speech_synthesizer
from directives import to_directive, generate_payload
from hyper import HTTP20Connection as HTTPConnection
from util import request_new_tokens, is_directive, multipart_parse

logger = logging.getLogger(__name__)
_PING_RATE = 300
_RECOGNIZE_METADATA_PART_HEADER = b'Content-Disposition: form-data; name="metadata"\nContent-Type: application/json; ' \
                                  b'charset=UTF-8\n\n'
_RECOGNIZE_AUDIO_PART_HEADER = b'Content-Disposition: form-data; name="audio"\nContent-Type: ' \
                               b'application/octet-stream\n\n'


class AVS:
    """
    AVS client. creates and maintains a connection to AVS and provides methods to handle directives and send events.
    """
    def __init__(self,
                 version,
                 access_token,
                 refresh_token,
                 client_id,
                 client_secret,
                 audio_device,
                 host='avs-alexa-na.amazon.com'):
        """
        connects to AVS and synchronizes state

        :param version: str AVS API version. ex. 'v20160207'
        :param access_token: str
        :param refresh_token: str
        :param client_id: str
        :param client_secret: str
        :param host: str hostname to connect to (always https on 443). defaults to 'avs-alexa-na.amazon.com'
        """
        self.version = version
        self.host = host
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._volume = 20
        self._muted = False
        self._alerts = []
        self._directives = []
        self.player = audio_player.Player(self)
        self._speech_token = None
        self._speech_state = speech_synthesizer.FINISHED
        # scheduler is threadsafe as of 3.3 (https://docs.python.org/3/library/sched.html)
        self.scheduler = sched.scheduler()
        self._mic_stop_event = None
        self.audio_device = audio_device

        logger.info("Connecting...")
        # we have to force protocol to http2 here because the ALPN is failing or something
        self._connection = HTTPConnection(self.host, 443, enable_push=True, force_proto='h2')
        logger.info("Connected")
        self.scheduler.enter(_PING_RATE, 1, self.send_ping)
        logger.info("Establishing downchannel stream...")
        self._downchannel_stream_id, self._dc_resp = self._establish_downstream_directives_channel()
        logger.info("Established downchannel stream")
        logger.info("Synchronizing state with AVS...")
        self.handle_parts(self.send_event_parse_response(generate_payload(self._generate_synchronize_state_event())))
        logger.info("Synchronized state with AVS")

    def _get_alert_state(self):
        """
        helper function providing Alerts state for context construction

        :return: dict Alerts context
        """
        return {
            "header": {
                "namespace": "Alerts",
                "name": "AlertsState"
            },
            "payload": {
                "allAlerts": self._alerts,
                "activeAlerts": [alert for alert in filter(lambda x: x.is_active(), self._alerts)]
            }
        }

    def _get_volume_state(self):
        """
        helper function providing Speaker state for context construction

        :return: dict Speaker context
        """
        return {
            "header": {
                "namespace": "Speaker",
                "name": "VolumeState"
            },
            "payload": {
                "volume": self._volume,
                "muted": self._muted
            }
        }

    def _get_playback_state(self):
        """
        helper function providing AudioPlayer state for context construction

        :return: dict AudioPlayer context
        """

        currently_playing = self.player.get_currently_playing()
        return {
            "header": {
                "namespace": "AudioPlayer",
                "name": "PlaybackState"
            },
            "payload": {
                "token": (currently_playing.stream.token if currently_playing else '') or '',
                "offsetInMilliseconds": self._get_playback_offset(),
                "playerActivity": self.player.get_state()
            }
        }

    def _get_speech_state(self):
        """
        helper function providing SpeechSynthesizer state for context construction

        :return: dict SpeechSynthesizer context
        """
        return {
            "header": {
                "namespace": "SpeechSynthesizer",
                "name": "SpeechState"
            },
            "payload": {
                "token": self._speech_token or '',
                "offsetInMilliseconds": self._get_speech_offset(),
                "playerActivity": self._speech_state
            }
        }

    def _generate_context(self):
        """
        https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/context

        :return: dict Context
        """
        return [
            self._get_volume_state(),
            self._get_alert_state(),
            self._get_playback_state(),
            self._get_speech_state()
        ]

    def _make_request(self, method, endpoint, body=None, headers=None, read=False, close=True, raises=True):
        """
        request helper function. adds authorization header, wraps body in a chunked iterable, and streams request
        to server chunk by chunk. a chunked send is used so that locks are released between each chunk, allowing
        the downchannel stream to receive data. this is critical to preventing deadlock when we are streaming the
        microphone in a Recognize event and are waiting for the StopCapture directive.

        :param method: str http method, eg. 'POST'
        :param endpoint: str AVS API endpoint, eg. 'events'
        :param body: file-like or iterable. if file-like, chunks of 320 bytes will be sent. if iterable, each yielded
            value will be sent.
        :param headers: dict http headers to send with request. note that 'authorization' is set automatically
        :param read: bool whether to read-out response. response content will be lost if True
        :param close: bool whether to close response. response content will be lost if True
        :param raises: bool whether raise an exception if response status code not in [200, 204]
        :return: tuple of stream ID and http response
        :raises AssertionError: if `raises`, raised when response status code is not in [200, 204]
        """
        if not headers:
            local_headers = {}
        else:
            local_headers = dict(headers)
        local_headers['authorization'] = 'Bearer {}'.format(self._access_token)
        # TODO: not every request needs to be chunked. afaik only the Recognize request with NEAR/FAR_FIELD requires it
        if body and not hasattr(body, '__iter__'):
            class ChunkIterable:
                def __init__(self, data):
                    self._data = data

                def __iter__(self):
                    def my_iterator():
                        while True:
                            ret = self._data.read(320)
                            if len(ret) < 320:
                                break
                            else:
                                yield ret
                        yield ret
                    return my_iterator()

            iterator = ChunkIterable(body)
        else:
            iterator = body
        stream_id = self._connection.request_chunked(method,
                                                     '/{}/{}'.format(self.version, endpoint),
                                                     iterator,
                                                     local_headers)
        # else:
        #     stream_id = self._connection.request(method, '/{}/{}'.format(self.version, endpoint), body, local_headers)
        response = self._connection.get_response(stream_id)
        if raises:
            assert response.status in [200, 204], "{} {}".format(response.status, response.read().decode())
        if read:
            response.read()
        if close:
            response.close()
        return stream_id, response

    def _generate_synchronize_state_event(self):
        """
        https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/system#synchronizestate

        :return: dict event payload
        """
        return {
            "context": self._generate_context(),
            "event": {
                "header": {
                    "namespace": "System",
                    "name": "SynchronizeState",
                    "messageId": str(uuid.uuid4())
                },
                "payload": {
                }
            }
        }

    def _generate_recognize_speech_event(self, profile):
        """
        https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/speechrecognizer#recognize

        :param profile: str ASR profile to use, eg. CLOSE_TALK or NEAR_FIELD
        :return: dict event payload
        """
        return {
            "context": self._generate_context(),
            "event": {
                "header": {
                    "namespace": "SpeechRecognizer",
                    "name": "Recognize",
                    "messageId": str(uuid.uuid4()),
                    "dialogRequestId": str(uuid.uuid4())
                },
                "payload": {
                    "profile": profile,
                    "format": "AUDIO_L16_RATE_16000_CHANNELS_1"
                }
            }
        }

    def _generate_alert_started_event(self, alert):
        """
        https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/alerts#alertstarted

        :param alert: Alert that was just started
        :return:
        """
        return {
            "event": {
                "header": {
                    "namespace": "Alerts",
                    "name": "AlertStarted",
                    "messageId": str(uuid.uuid4())
                },
                "payload": {
                    "token": alert.token
                }
            }
        }

    def send_event_parse_response(self, payload):
        """
        wrapper method to make event request with payload as content and parse response into parts (assuming multipart
        response)

        :param payload: file-like or iterable
        :return: list of BodyPart elements
        """
        logger.info("Sending event request...")
        logger.info("Context: {}".format(json.dumps(self._generate_context())))
        ret = []
        try:
            _, resp = self._make_request('POST', 'events', payload, {'Content-Type': payload.content_type}, close=False)
            logger.info("Sent event request")
            logger.info("Retrieving event response...")
            if 'content-type' in resp.headers:
                ret = multipart_parse(resp.read(), resp.headers['content-type'][0].decode())
            logger.info("Retrieved event response")
            resp.close()
        except StreamClosedError:
            logger.exception("Stream closed during event send: {}".format(payload))

        return ret

    def handle_parts(self, parts):
        """
        Process BodyParts of multipart response as directives and non-directives (or content). associates content
        with corresponding directive (if any), calls on_receive for each directive, and adds the directives to the
        directive list for final processing later.

        :param parts: list of BodyPart
        """
        logging.debug("directives before before: {}".format(self._directives))
        directives = [to_directive(data) for headers, data in filter(lambda x: is_directive(x[0], x[1]), parts)]
        non_directives = [(headers, data) for headers, data in filter(lambda x: not is_directive(x[0], x[1]), parts)]

        def consume_content(headers, data, _directives):
            for _directive in (d for d in _directives if d):
                if _directive.content_handler(headers, data):
                    break
            else:
                return False
            return True

        if not all(consume_content(headers, data, directives) for headers, data in non_directives):
            logger.warning("left over contents")
        for directive in (d for d in directives if d):
            directive.on_receive(self)
        # TODO check if extend is thread-safe
        self._directives.extend(d for d in directives if d)
        logging.debug("directives after after: {}".format(self._directives))

    def _handle_directives(self):
        """
        process outstanding directives
        """
        if self._directives:
            logging.debug("directives before: {}".format(self._directives))
            for directive in list(self._directives):
                if not directive:
                    self._directives.remove(directive)
                if directive.handle(self):
                    self._directives.remove(directive)
            logging.debug("directives after: {}".format(self._directives))

    def _generate_recognize_payload(self, audio):
        """
        prepare event payload for speech Recognize event. if the audio file-like does not specify a total length via
        __len__, len, or similar, it is assumed that audio is a continuous stream. in this case the NEAR_FIELD profile
        is used and a custom file-like wrapper is constructed to deliver the multi-part body. however, if a total
        length of the audio can be determined, the CLOSE_TALK profile will be used.

        :param audio: file-like containing audio for request.
        :return: file-like containing the payload for the http request
        """
        if total_len(audio) is None:
            event = self._generate_recognize_speech_event('NEAR_FIELD')
            boundary_term = str(uuid.uuid4())
            boundary_separator = b'--' + boundary_term.encode('utf8') + b'\n'
            body = b''.join([boundary_separator,
                             _RECOGNIZE_METADATA_PART_HEADER,
                             json.dumps(event).encode('utf8'),
                             b'\n\n',
                             boundary_separator,
                             _RECOGNIZE_AUDIO_PART_HEADER])
            epilogue = b'\n\n--' + boundary_term.encode('utf8') + b'--'

            class MultiPartAudioFileLike:
                def __init__(self):
                    self._audio_closed = False
                    self.content_type = 'multipart/form-data; boundary={}'.format(boundary_term)
                    self._of = open('recognize_request.txt', 'wb')

                def read(self, size=-1):
                    nonlocal body
                    nonlocal epilogue
                    ret = b''
                    if len(body):
                        ret = body[:size]
                        body = body[size:]
                    size -= len(ret)
                    if size and not self._audio_closed:
                        audio_data = audio.read(size)
                        if len(audio_data) < size:
                            self._audio_closed = True
                        ret += audio_data
                        size -= len(audio_data)
                    if size:
                        if len(epilogue):
                            ret += epilogue[:size]
                            epilogue = epilogue[size:]
                    self._of.write(ret)
                    return ret

            return MultiPartAudioFileLike()
        else:
            event = self._generate_recognize_speech_event('CLOSE_TALK')
            payload = MultipartEncoder({
                'metadata': (None, io.BytesIO(json.dumps(event).encode()), 'application/json'),
                'audio': (None, audio, 'application/octet-stream')
            })
        return payload

    def _establish_downstream_directives_channel(self):
        """
        establishes the downchannel directives stream. refreshes access token if expired.

        :return: tuple of downchannel stream id and the downchannel response object
        """
        ds_id, resp = self._make_request('GET', 'directives', close=False, raises=False)
        if resp.status == 403:
            resp.read()
            resp.close()

            def write_tokens_to_file(tokens):
                f = open('tokens.txt', 'w')
                f.write(json.dumps(tokens))
                f.close()

            self._access_token, self._refresh_token = request_new_tokens(self._refresh_token,
                                                                         self._client_id,
                                                                         self._client_secret,
                                                                         write_tokens_to_file)
            return self._establish_downstream_directives_channel()
        else:
            return ds_id, resp

    def recognize_speech(self, speech, mic_stop_event):
        """
        send recognize speech event and process the response

        :param speech: file-like containing speech for request
        :param mic_stop_event: threading.Event when speech is an infinite stream, to monitor for signal from
            downchannel stream to end the recognize request. when speech is finite, can be None
        """
        self._mic_stop_event = mic_stop_event
        self.handle_parts(self.send_event_parse_response(self._generate_recognize_payload(speech)))

    def _get_playback_offset(self):
        """
        TODO: method to get playback offset from AudioPlayer interface

        :return: int offset in milliseconds
        """
        return 0

    def _get_speech_offset(self):
        """
        TODO: method to get playback offset from SpeechSynthesizer interface

        :return: int offset in milliseconds
        """
        return 0

    def send_ping(self):
        """
        self-scheduling task to send http2 PING every _PING_RATE seconds
        """
        logger.debug("PINGING AVS")
        self._connection.ping(b'\x00' * 8)
        logger.info("PINGED AVS")
        self.scheduler.enter(_PING_RATE, 1, self.send_ping)

    def run(self):
        """
        main loop for AVS client

        1. checks for any expired scheduled tasks that need to run
        2. handles outstanding directives
        3. runs one iteration of audio player state-machine loop

        :return:
        """
        self.scheduler.run(blocking=False)
        self._handle_directives()
        self.player.run()

    def play_alert(self, alert):
        """
        plays alert audio file infinitely using _audio_device. sends AlertStartedEvent to indicate `alert` has been
        started. if alert.type is:
            - 'ALARM', plays 'alert.wav'
            - 'TIMER', plays 'timer.wav'

        :param alert: Alert to start
        """
        self.handle_parts(self.send_event_parse_response(generate_payload(self._generate_alert_started_event(alert))))
        alert._active = True
        logger.info("PLAYING {}: {}".format(alert.type, alert.token))
        audio_filename = 'alarm.wav' if alert.type == 'ALARM' else 'timer.wav' if alert.type == 'TIMER' else None
        alert._process = self.audio_device.play_infinite(audio_filename)

    def stop_capture(self):
        """
        called by downchannel directive stream when handling StopCapture directive. signals mic input capturing thread
        to stop capture via _mic_stop_event.
        """
        if self._mic_stop_event:
            self._mic_stop_event.set()

    def add_alert(self, alert):
        """
        add an alert to the list of created alerts

        :param alert: Alert
        """
        # TODO: make atomic (may already be on CPython but still)
        self._alerts.append(alert)

    def get_alert(self, token):
        """
        retrieve an alert from the list of created alerts

        :param token: str unique alert identifier
        :return: Alert
        :raises StopIteration: if no matching alert is found
        """
        return next(a for a in self._alerts if a.token == token)

    def remove_alert(self, alert):
        """
        remove an alert from the list of created alerts

        :param alert: Alert to remove
        """
        self._alerts.remove(alert)
