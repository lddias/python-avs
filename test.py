import logging
import sys
import subprocess
import json
import queue
import threading
import shutil

import pyaudio

import snowboydecoder
from speech_recognizer import AudioInputDevice
import avs
from audio_player import AudioDevice


class MplayerAudioDevice(AudioDevice):
    def __init__(self, binary_path, options=None):
        self._binary_path = binary_path
        self._paused = False
        self._options = options or []

    def check_exists(self):
        return shutil.which(self._binary_path)

    def play_once(self, file, playlist=False):
        try:
            return subprocess.Popen([self._binary_path] + self._options + (['-playlist'] if playlist else []) + [file],
                                    stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception:
            logger.exception("Couldn't play audio")

    def play_infinite(self, file):
        try:
            return subprocess.Popen(
                [self._binary_path] + self._options + ["-loop", "0", file],
                stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.STDOUT)
        except:
            logger.exception("Couldn't play audio")

    def stop(self, p):
        p.communicate(input=b'quit 0\n')

    def pause(self, p):
        if not self._paused:
            p.communicate(input=b'pause\n')
            self._paused = True

    def resume(self, p):
        if self._paused:
            p.communicate(input=b'pause\n')
            self._paused = False

    def ended(self, p):
        return p.poll() is not None


class PyAudioInputDevice(AudioInputDevice):
    def start_recording(self):
        audio = pyaudio.PyAudio()

        # start Recording
        stream = audio.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=1024)
        logger.info("recording...")

        self._audio = audio
        self._stream = stream
        self._stopped = False
        self._event = threading.Event()

    def read(self, size=-1):
        if self._event.is_set():
            logger.info("MIC STOP REQUESTED")
            self._stopped = True
            self._stream.stop_stream()
            self._stream.close()
            self._audio.terminate()
            self._event.clear()
        if self._stopped:
            logger.warning("READING FROM MIC WHILE CLOSED")
            return b''
        try:
            return self._stream.read(size, exception_on_overflow=False)
        except:
            logger.exception("exception while reading from pyaudio stream")
            self._stopped = True
            try:
                self._stream.stop_stream()
                self._stream.close()
                self._audio.terminate()
            except:
                pass
            return b''

    def stop_recording(self):
        self._event.set()


def hotword_detect(logger, q, mic_stopped):
    interrupted = False

    def hotword_detected_callback():
        snowboydecoder.play_audio_file()
        nonlocal interrupted
        interrupted = True

    def interrupt_check_callback():
        return interrupted

    detector = snowboydecoder.HotwordDetector('resources/alexa.umdl', sensitivity=0.5)
    logger.info("waiting for hotword...")
    detector.start(detected_callback=hotword_detected_callback,
                   interrupt_check=interrupt_check_callback,
                   sleep_time=0.03)
    detector.terminate()

    q.put(('hotword',))


def start_hotword_detection_thread(q):
    hdt = threading.Thread(target=hotword_detect, name='Hotword Detection Thread', args=(logger, q, mic_stopped))
    hdt.setDaemon(False)
    hdt.start()


if __name__ == '__main__':
    # clear root logger handlers
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    # set new root logger handlers
    logging.basicConfig(stream=sys.stdout,
                        format='[%(asctime)s][%(threadName)s][%(levelname)-5.5s][%(pathname)s:%(lineno)d] %(message)s',
                        level=logging.DEBUG)

    # when we log below WARNING, these libraries are a bit too verbose for me
    logging.getLogger('hpack').setLevel(logging.WARNING)
    logging.getLogger('hyper').setLevel(logging.WARNING)
    logger = logging.getLogger(__name__)

    logger.info("STARTING ALEXA APP")
    tokens = json.load(open('tokens.txt'))
    secrets = json.load(open('secrets.txt'))
    q = queue.Queue()
    audio_devices = [MplayerAudioDevice('mplayer', ["-ao", "alsa", "-really-quiet", "-noconsolecontrols", "-slave"]),
                     MplayerAudioDevice('/Applications/MPlayer OSX Extended.app/Contents/Resources/Binaries/mpextended.mpBinaries/Contents/MacOS/mplayer', ["-really-quiet", "-noconsolecontrols", "-slave"])]
    a = avs.AVS('v20160207',
                tokens.get('access_token'),
                tokens.get('refresh_token'),
                secrets.get('client_id'),
                secrets.get('client_secret'),
                next(audio_device for audio_device in audio_devices if audio_device.check_exists()),
                PyAudioInputDevice(),
                'NEAR_FIELD')

    mic_stopped = threading.Event()

    start_hotword_detection_thread(q)
    while True:
        try:
            job = q.get(block=False)
            if job[0] == 'hotword':
                logger.info("STARTING RECOGNIZE SPEECH")
                a.recognize_speech()
                logger.info("FINISHED RECOGNIZE SPEECH")
                start_hotword_detection_thread(q)
            else:
                logger.error("unknown command: {}".format(job))
        except queue.Empty:
            pass
        a.run()
