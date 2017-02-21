import logging
import uuid

from directives import generate_payload

logger = logging.getLogger(__name__)

# audio player states
IDLE = 'IDLE'
PLAYING = 'PLAYING'
BUFFER_UNDERRUN = 'BUFFER_UNDERRUN'
FINISHED = 'FINISHED'
STOPPED = 'STOPPED'
PAUSED = 'PAUSED'


def generate_playback_started_event(token, offset=0):
    """
    https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/audioplayer#playbackstarted

    :param token: str token of the previous content
    :param offset: int offset in milliseconds of the previous content
    :return: dict event payload
    """
    return {
        "event": {
            "header": {
                "namespace": "AudioPlayer",
                "name": "PlaybackStarted",
                "messageId": str(uuid.uuid4()),
            },
            "payload": {
                "token": token,
                "offsetInMilliseconds": offset
            }
        }
    }


def generate_playback_nearly_finished_event(token, offset=0):
    """
    https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/audioplayer#playbacknearlyfinished

    :param token: str token of the current content
    :param offset: int offset in milliseconds of the current content
    :return: dict event payload
    """
    return {
        "event": {
            "header": {
                "namespace": "AudioPlayer",
                "name": "PlaybackNearlyFinished",
                "messageId": str(uuid.uuid4()),
            },
            "payload": {
                "token": token,
                "offsetInMilliseconds": offset
            }
        }
    }


def generate_playback_finished_event(token, offset=0):
    """
    https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/audioplayer#playbackfinished

    :param token: str token of the previous content
    :param offset: int offset in milliseconds of the previous content
    :return: dict event payload
    """
    return {
        "event": {
            "header": {
                "namespace": "AudioPlayer",
                "name": "PlaybackStarted",
                "messageId": str(uuid.uuid4()),
            },
            "payload": {
                "token": token,
                "offsetInMilliseconds": offset
            }
        }
    }


def generate_playback_stopped_event(token, offset=0):
    """
    https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/audioplayer#playbackstopped

    :param token: str token of the previous content
    :param offset: int offset in milliseconds of the previous content
    :return: dict event payload
    """
    return {
        "event": {
            "header": {
                "namespace": "AudioPlayer",
                "name": "PlaybackStopped",
                "messageId": str(uuid.uuid4())
            },
            "payload": {
                "token": token,
                "offsetInMilliseconds": offset
            }
        }
    }


def generate_playback_queue_cleared_event():
    """
    https://developer.amazon.com/public/solutions/alexa/alexa-voice-service/reference/audioplayer#playbackqueuecleared

    :return: dict event payload
    """
    return {
        "event": {
            "header": {
                "namespace": "AudioPlayer",
                "name": "PlaybackQueueCleared",
                "messageId": str(uuid.uuid4())
            },
            "payload": {
            }
        }
    }


class AudioDevice:
    """
    abstract class for audio device

    audio device is used for playing audio received from AVS or retrieved from links received from AVS
    """

    def check_exists(self):
        """
        :return: True if the audio device is available on this system, otherwise False
        """
        return False

    def play_once(self, file, playlist=False):
        """
        starts playback of the audiofile located at path `file`

        :param file: str path to the audio file that should be played once
        :return: handle to control audio playback via `stop`, `pause`, and `ended`
        """
        raise NotImplementedError

    def play_infinite(self, file):
        """
        starts playback of the audiofile located at path `file`. playback loops infinitely

        :param file: str path to the audio file that should be played once
        :return: handle to control audio playback via `stop`, `pause`, and `ended`
        """
        raise NotImplementedError

    def stop(self, p):
        """
        stop playback of the audio controlled by handle `p`
        :param p: handle to audio playback
        """
        raise NotImplementedError

    def pause(self, p):
        """
        pause playback of the audio controlled by handle `p`
        :param p: handle to audio playback
        """
        raise NotImplementedError

    def resume(self, p):
        """
        resume playback of the audio controlled by handle `p`
        :param p: handle to audio playback
        """
        raise NotImplementedError

    def ended(self, p):
        """
        :return: True if the audio controlled by handle `p` has finished playback, False otherwise
        """
        raise NotImplementedError


class Player:
    """
    audio player state machine
    """
    def __init__(self, avs):
        self._avs = avs
        self._state = IDLE
        self._currently_playing = None
        self._queue = []

    def get_currently_playing(self):
        return self._currently_playing

    def get_state(self):
        return self._state

    def _play(self, audio_item):
        """
        start playback of audio specified by `audio_item`. sends PlaybackStartedEvent. if 1 or fewer items are present
        in the queue, sends PlaybackNearlyFinishedEvent.

        :param audio_item: directives.AudioItem
        """
        payload = generate_payload(generate_playback_started_event(audio_item.stream.token))
        logging.debug("PLAYBACK STARTED RESPONSE: {}".format(self._avs.send_event_parse_response(payload)))
        audio_item._process = self._avs.audio_device.play_once(*audio_item.get_file_path())
        self._currently_playing = audio_item
        self._state = PLAYING
        # TODO: this is not really the condition to send nearly_finished according to the docs...
        if len(self._queue) <= 1:
            payload = generate_payload(generate_playback_nearly_finished_event(self._currently_playing.stream.token))
            self._avs.handle_parts(self._avs.send_event_parse_response(payload))

    def _item_finished(self):
        """
        helper function to check if an item being played has finished
        :return: True if an item being played has finished, False otherwise
        """
        return self._state == PLAYING and self._avs.audio_device.ended(self._currently_playing.process)

    def _item_playing(self):
        """
        helper function to check if an item is being played
        :return: True if an item is being played, False otherwise
        """
        return self._state == PLAYING and not self._avs.audio_device.ended(self._currently_playing.process)

    def run(self):
        """
        state machine progression:
            * if an item being played has finished, send PlaybackFinishedEvent and move to the Finished state
            * if in the Idle, Stopped, or Finished states, play the next item in the queue

        TODO: check if it encountered errors/buffer underrun
        """
        if self._item_finished():
            logging.debug("PLAYBACK FINISHED RESPONSE: {}".format(
                self._avs.send_event_parse_response(
                    generate_payload(generate_playback_finished_event(self._currently_playing.stream.token)))))
            logging.info("audio player state changing to: FINISHED")
            self._currently_playing = None
            self._state = FINISHED
        if self._state in [IDLE, STOPPED, FINISHED] and self._queue:
            self._play(self._queue.pop(0))

    def stop(self):
        """
        stop playback of the audio item being played, send PlaybackStoppedEvent and move to the Stopped state.
        """
        if self._item_playing():
            self._avs.audio_device.stop(self._currently_playing.process)
            self._avs.send_event_parse_response(generate_payload(generate_playback_stopped_event(
                self._currently_playing.stream.token if self._currently_playing else '')))
            self._state = STOPPED
        else:
            logger.warning("called stop() while not playing (state: {})".format(self._state))

    def enqueue(self, audio_item):
        """
        add an audio stream to the play queue
        :param audio_item: directives.AudioItem
        """
        self._queue.append(audio_item)

    def clear_queue(self):
        """
        clear the play queue. sends PlaybackQueueClearedEvent
        """
        self._queue.clear()
        self._avs.send_event_parse_response(generate_payload(generate_playback_queue_cleared_event()))

    def pause(self):
        # TODO
        raise NotImplementedError

    def resume(self):
        # TODO
        raise NotImplementedError
