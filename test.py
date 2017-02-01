import logging
import sys
import signal
import subprocess
import json
import queue
import threading
import datetime

import pyaudio
import shutil

import snowboydecoder
from debug import fake_mic2, fake_mic
from util import multipart_parse
import avs
from audio_player import AudioDevice


class MplayerAudioDevice(AudioDevice):
    def __init__(self):
        self._paused = False

    def check_exists(self):
        return shutil.which('mplayer')

    def play_once(self, file):
        try:
            return subprocess.Popen(["mplayer", "-ao", "alsa", "-really-quiet", "-noconsolecontrols", "-slave", file],
                                    stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception:
            logger.exception("Couldn't play audio")

    def play_infinite(self, file):
        try:
            return subprocess.Popen(
                ["mplayer", "-ao", "alsa", "-really-quiet", "-noconsolecontrols", "-slave", "-loop", "0", file],
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


class AfplayAudioDevice(AudioDevice):
    def check_exists(self):
        return shutil.which('afplay')

    def play_once(self, file):
        try:
            return subprocess.Popen(["afplay", file])
        except Exception:
            logger.exception("Couldn't play audio")

    def play_infinite(self, file):
        try:
            return subprocess.Popen(["while :; do afplay {}; done".format(file)], shell=True)
        except Exception:
            logger.exception("Couldn't play audio")

    def stop(self, p):
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()

    def pause(self, p):
        p.send_signal(signal.SIGSTOP)

    def resume(self, p):
        p.send_signal(signal.SIGCONT)

    def ended(self, p):
        return p.poll() is not None


def downstream_directives():
    # check directives
    for push in a._dc_resp.read_chunked():
        logger.info("[{}] DOWNSTREAM DIRECTIVE RECEIVED: {}".format(datetime.datetime.now().isoformat(), push))
        parts = multipart_parse(push, a._dc_resp.headers['content-type'][0].decode())
        a.handle_parts(parts)
    # TODO: reconnect when this happens
    logger.warning("downstream finished read_chunked!")


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

    paudio = pyaudio.PyAudio()

    # start Recording
    mic_stream = paudio.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=1024)
    logger.info("recording...")

    class StoppableAudioStream:
        def __init__(self, audio, stream):
            self._audio = audio
            self._stream = stream
            self._stopped = False

        def read(self, size=-1):
            if mic_stopped.is_set():
                logger.info("MIC STOP REQUESTED")
                self._stopped = True
                self._stream.stop_stream()
                self._stream.close()
                self._audio.terminate()
                mic_stopped.clear()
            if self._stopped:
                logger.warning("READING FROM MIC WHILE CLOSED")
                return b''
            # workaround for pyaudio versions before exception_on_overflow=False
            while True:
                try:
                    return self._stream.read(size)
                except:
                    logger.exception("exception while reading from pyaudio stream")

    q.put(('hotword', StoppableAudioStream(paudio, mic_stream), mic_stopped))


def start_hotword_detection_thread():
    hdt = threading.Thread(target=hotword_detect, name='Hotword Detection Thread', args=(logger, q, mic_stopped))
    hdt.setDaemon(True)
    hdt.start()


if __name__ == '__main__':
    # clear root logger handlers
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    # set new root logger handlers
    logging.basicConfig(stream=sys.stdout,
                        format='[%(asctime)s][%(threadName)s][%(levelname)-5.5s][%(pathname)s:%(lineno)d] %(message)s',
                        level=logging.INFO)

    # when we log below WARNING, these libraries are a bit too verbose for me
    logging.getLogger('hpack').setLevel(logging.WARNING)
    logging.getLogger('hyper').setLevel(logging.WARNING)
    logger = logging.getLogger(__name__)

    logger.info("STARTING ALEXA APP")
    tokens = json.load(open('tokens.txt'))
    secrets = json.load(open('secrets.txt'))
    q = queue.Queue()
    audio_devices = [MplayerAudioDevice(), AfplayAudioDevice()]
    a = avs.AVS('v20160207',
                tokens.get('access_token'),
                tokens.get('refresh_token'),
                secrets.get('client_id'),
                secrets.get('client_secret'),
                next(audio_device for audio_device in audio_devices if audio_device.check_exists()))

    mic_stopped = threading.Event()

    ddt = threading.Thread(target=downstream_directives, name='Downstream Directives Thread')
    ddt.setDaemon(False)
    ddt.start()

    start_hotword_detection_thread()
    while True:
        try:
            job = q.get(block=False)
            if job[0] == 'hotword':
                logger.info("STARTING RECOGNIZE SPEECH")
                a.recognize_speech(job[1], job[2])
                logger.info("FINISHED RECOGNIZE SPEECH")
                start_hotword_detection_thread()
            else:
                logger.error("unknown command: {}".format(job))
        except queue.Empty:
            pass
        a.run()
