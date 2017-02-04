import base64
import datetime
import io
import logging
import ujson as json
import uuid

import dateutil.parser
import pytz
import requests
from requests_toolbelt import MultipartEncoder

import speech_synthesizer

logger = logging.getLogger(__name__)


def to_directive(data):
    """
    constructs Directive class from part JSON object content (of multi-part http response)

    WARNING: this function uses `eval` on the contents of data['directive']['header'] keys 'namespace' and 'name'

    :param data: dict part JSON object content
    :return: Directive sub-class
    """
    directive_identifier = '{}.{}'.format(data['directive']['header']['namespace'], data['directive']['header']['name'])
    try:
        return eval(directive_identifier)(data)
    except (NameError, AttributeError):
        # TODO: send ExceptionEncountered event
        logger.warning("Unknown directive: {}".format(directive_identifier))
    except Exception:
        # TODO: send ExceptionEncountered event
        logger.exception("Error initializing directive {} with data {}".format(directive_identifier, data))


def generate_payload(event):
    """
    returns a file-like MultipartEncoder instance that can be used to write-out the multi-part request headers and body


    :param event: dict payload to send as "metadata" part in multi-part request
    :return: MultipartEncoder
    """
    return MultipartEncoder({"metadata": (None, io.BytesIO(json.dumps(event).encode()), 'application/json')})


class Directive:
    """
    Base-class for directives.
    """
    def __init__(self, data):
        assert 'directive' in data, "Invalid directive payload, 'directive' key not present"
        self._received_at = datetime.datetime.now()
        self._namespace = data['directive']['header']['namespace']
        self.name = data['directive']['header']['name']
        self.message_id = data['directive']['header']['messageId']
        self.dialogRequestId = data['directive']['header'].get('dialogRequestId')

    def on_receive(self, avs):
        """
        action to perform as soon as directive is received.
        """
        pass

    def content_handler(self, headers, content):
        """
        check and retain reference to headers and content, if this directive is responsible for this content

        :param headers: dict of part (of multi-part http response) headers (from network, bytes keys/values)
        :param content: bytes of part (of multi-part http response) content
        :return: True if responsible for content, False otherwise
        """
        return False

    def handle(self, avs):
        """
        complete directive, if possible. this may be called sometime after the directive is actually received. should
        only be called from the main thread

        :param avs: AVS instance
        :return: True if directive completed, False otherwise
        """
        return True

    def __repr__(self):
        return '<{} @ {}>'.format(self.__class__.__name__, self._received_at)


class SpeechSynthesizer:
    """
    SpeechSynthesizer namespace directives
    """
    class Speak(Directive):
        """
        https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/speechsynthesizer#speak
        """
        def __init__(self, data):
            super().__init__(data)
            url = data['directive']['payload']['url']
            content_id_identifier = 'cid:'
            assert url.startswith(content_id_identifier)
            self.content_id = url[len(content_id_identifier):]
            self.format = data['directive']['payload']['format']
            self.token = data['directive']['payload']['token']
            self._audio = None

        def _generate_speech_started_event(self):
            """
            https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/speechsynthesizer#speechstarted

            :return: dict event payload
            """
            return {
                "event": {
                    "header": {
                        "namespace": "SpeechSynthesizer",
                        "name": "SpeechStarted",
                        "messageId": str(uuid.uuid4()),
                    },
                    "payload": {
                        "token": self.token
                    }
                }
            }

        def _generate_speech_finished_event(self):
            """
            https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/speechsynthesizer#speechfinished

            :return: dict event payload
            """
            return {
                "event": {
                    "header": {
                        "namespace": "SpeechSynthesizer",
                        "name": "SpeechFinished",
                        "messageId": str(uuid.uuid4()),
                    },
                    "payload": {
                        "token": self.token
                    }
                }
            }

        def content_handler(self, headers, content):
            if self.content_id.encode() in headers.get(b'Content-ID', b''):
                self._audio = content
                return True
            return False

        def handle(self, avs):
            if self._audio:
                logger.info("handling Speak directive")
                # send SpeechStarted event
                avs.send_event_parse_response(generate_payload(self._generate_speech_started_event()))
                # play speech
                # TODO: handle channel interactions
                avs._speech_token = self.token
                avs._speech_state = speech_synthesizer.PLAYING
                open('/tmp/response.mp3', 'wb').write(self._audio.encode('latin1'))
                avs.audio_device.play_once("/tmp/response.mp3")
                avs._speech_state = speech_synthesizer.FINISHED
                # send SpeechEnded event
                avs.send_event_parse_response(generate_payload(self._generate_speech_finished_event()))
                return True
            else:
                logger.warning("unable to handle Speak directive, no audio content")
                return False


class SpeechRecognizer:
    """
    SpeechRecognizer namespace directives
    """
    class StopCapture(Directive):
        """
        https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/speechrecognizer#stopcapture
        """
        def __init__(self, data):
            super().__init__(data)
            self.immediate_safe = True

        def on_receive(self, avs):
            avs.stop_capture()


class Alert:
    """
    Alert data-structure

    The member names of this class are chosen so that JSON de-serialization via the `ujson` module yields the desired
    results (https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/context#alertsstate).
    `ujson` handles de-serialization of arbitrary classes by either calling the instance's toDict method if it exists,
    otherwise it creates a JSON object of the unprotected class and instance variables that it can serialize.
    """
    def __init__(self, token, alert_type, scheduled_time):
        self.token = token
        self.type = alert_type
        self.scheduledTime = scheduled_time
        self._active = False
        self._process = None

    def is_active(self):
        return self._active

    def set_active(self, active):
        self._active = active

    def get_process(self):
        return self._process

    def set_process(self, p):
        self._process = p


class Alerts:
    """
    Alerts namespace directives
    """
    class SetAlert(Directive):
        """
        https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/alerts#setalert
        """
        def __init__(self, data):
            super().__init__(data)
            self.token = data['directive']['payload']['token']
            self.type = data['directive']['payload']['type']
            self.scheduledTime = dateutil.parser.parse(data['directive']['payload']['scheduledTime'])
            self._alert = Alert(self.token, self.type, data['directive']['payload']['scheduledTime'])

        def _generate_set_alert_succeeded_event(self):
            """
            https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/alerts#setalertsucceeded

            :return: dict event payload
            """
            return {
                "event": {
                    "header": {
                        "namespace": "Alerts",
                        "name": "SetAlertSucceeded",
                        "messageId": str(uuid.uuid4()),
                    },
                    "payload": {
                        "token": self.token
                    }
                }
            }

        def content_handler(self, headers, content):
            return False

        def handle(self, avs):
            logger.info("ADDING ALERT {}".format(self._alert))
            avs.add_alert(self._alert)
            # scheduler.enter takes the delay in time units from now
            # AVS alerts have an ISO8601 scheduledTime which we assume has a timezone
            delay = (self.scheduledTime - datetime.datetime.utcnow().replace(tzinfo=pytz.UTC)).seconds + 1
            avs.scheduler.enter(delay, 1, avs.play_alert, [self._alert])
            avs.send_event_parse_response(generate_payload(self._generate_set_alert_succeeded_event()))
            return True

    class DeleteAlert(Directive):
        """
        https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/alerts#deletealert
        """
        def __init__(self, data):
            super().__init__(data)
            self.token = data['directive']['payload']['token']

        def _generate_delete_alert_succeeded_event(self):
            """
            https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/alerts#deletealertsucceeded

            :return: dict event payload
            """
            return {
                "event": {
                    "header": {
                        "namespace": "Alerts",
                        "name": "DeleteAlertSucceeded",
                        "messageId": str(uuid.uuid4()),
                    },
                    "payload": {
                        "token": self.token
                    }
                }
            }

        def _generate_alert_stopped_event(self):
            """
            https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/alerts#alertstopped

            :return: dict event payload
            """
            return {
                "event": {
                    "header": {
                        "namespace": "Alerts",
                        "name": "AlertStopped",
                        "messageId": str(uuid.uuid4()),
                    },
                    "payload": {
                        "token": self.token
                    }
                }
            }

        def content_handler(self, headers, content):
            return False

        def handle(self, avs):
            alert = avs.get_alert(self.token)
            avs.audio_device.stop(alert.get_process())
            avs.send_event_parse_response(generate_payload(self._generate_alert_stopped_event()))
            avs.remove_alert(alert)
            avs.send_event_parse_response(generate_payload(self._generate_delete_alert_succeeded_event()))
            return True


class AudioItem:
    """
    Audio Item data-structure

    """
    class Stream:
        """
        Audio Item Stream data-structure
        """
        def __init__(self,
                     url,
                     stream_format,
                     offset_in_milliseconds,
                     expiry_time,
                     progress_report_delay_in_milliseconds,
                     progress_report_interval_in_milliseconds,
                     token,
                     expected_previous_token):
            self.url = url
            content_id_identifier = 'cid:'
            if url.startswith(content_id_identifier):
                self.content_id = url[len(content_id_identifier):]
            else:
                self.content_id = None
            self.stream_format = stream_format
            self.offset_in_milliseconds = offset_in_milliseconds
            self.expiry_time = expiry_time
            self.progress_report_delay_in_milliseconds = progress_report_delay_in_milliseconds
            self.progress_report_interval_in_milliseconds = progress_report_interval_in_milliseconds
            self.token = token
            self.expected_previous_token = expected_previous_token

    def __init__(self,
                 audio_item_id,
                 url,
                 stream_format,
                 offset_in_milliseconds,
                 expiry_time,
                 progress_report_delay_in_milliseconds,
                 progress_report_interval_in_milliseconds,
                 token,
                 expected_previous_token):
        self._id = audio_item_id
        self.stream = AudioItem.Stream(url, stream_format, offset_in_milliseconds, expiry_time,
                                       progress_report_delay_in_milliseconds, progress_report_interval_in_milliseconds,
                                       token, expected_previous_token)
        self._audio = None
        self._process = None

    @property
    def process(self):
        return self._process

    @process.setter
    def process(self, p):
        self._process = p

    def get_file_path(self):
        """
        Stores the content to a temporary file in /tmp, named by base64 encoding either the content_id or a uniquely
        generated ID, and return the path to the file.
        :return: str path to audio file
        """
        if self.stream.content_id:
            if self._audio:
                filename = '/tmp/{}.mp3'.format(base64.urlsafe_b64encode(self.stream.content_id.encode()))
                open(filename, 'wb').write(self._audio.encode('latin1'))
            else:
                logger.warning("unable to retrieve filename, no audio content")
                return None
        else:
            filename = '/tmp/{}.mp3'.format(base64.urlsafe_b64encode(str(uuid.uuid4()).encode()))
            s = requests.session()
            r = s.get(self.stream.url)
            logger.debug('content type: {} content: {}'.format(r.headers.get('Content-Type'), r.content))
            if 'audio/x-mpegurl' in r.headers.get('Content-Type', ''):
                r = s.get(next(r.iter_lines()))
            open(filename, 'wb').write(r.content)
        return filename


class AudioPlayer:
    """
    AudioPlayer namespace directives
    """
    class Play(Directive):
        """
        https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/audioplayer#play
        """
        def __init__(self, data):
            super().__init__(data)
            self.play_behavior = data['directive']['payload']['playBehavior']
            ai = data['directive']['payload']['audioItem']
            s = ai['stream']
            self.audio_item = AudioItem(ai['audioItemId'],
                                        s.get('url'),
                                        s.get('streamFormat'),
                                        s.get('offsetInMilliseconds'),
                                        s.get('expiryTime'),
                                        s.get('progressReport', {}).get('progressReportDelayInMilliseconds'),
                                        s.get('progressReport', {}).get('progressReportIntervalInMilliseconds'),
                                        s.get('token'),
                                        s.get('expectedPreviousToken'))

        def content_handler(self, headers, content):
            if self.audio_item.stream.content_id:
                if self.audio_item.stream.content_id.encode() in headers.get(b'Content-ID', b''):
                    self.audio_item._audio = content
                    return True
            return False

        def handle(self, avs):
            logging.debug("Handling AudioPlayer Play directive: {}".format(json.dumps(self.audio_item)))
            if self.play_behavior == 'REPLACE_ALL':
                avs.player.stop()
                avs.player.clear_queue()
                avs.player.enqueue(self.audio_item)
            elif self.play_behavior == 'ENQUEUE':
                avs.player.enqueue(self.audio_item)
            elif self.play_behavior == 'REPLACE_ENQUEUED':
                avs.player.clear_queue()
                avs.player.enqueue(self.audio_item)
            else:
                logger.warning("Unknown play behavior received: {}".format(self.play_behavior))
            return True

    class Stop(Directive):
        """
        https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/audioplayer#stopdirective
        """
        def handle(self, avs):
            logging.debug("Handling AudioPlayer Stop directive")
            avs.player.stop()
            return True

    class ClearQueue(Directive):
        """
        https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/audioplayer#clearqueue
        """
        def __init__(self, data):
            super().__init__(data)
            self.clear_behavior = data['directive']['payload']['clearBehavior']

        def handle(self, avs):
            if self.clear_behavior == 'CLEAR_ALL':
                avs.player.stop()
            avs.player.clear_queue()
            return True
