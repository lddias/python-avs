# python-avs
A Python3 client for AVS API v20160207

All\* AVS directives are supported. You can even send speech Recognize requests with streaming microphone input (NEAR/FAR_FIELD profiles).
## Usage

1. Create AVS client
    ```python
    import avs
    
    a = avs.AVS('v20160207', 'access_token', 'refresh_token', 'client_id', 'client_secret', audio_device)
    ```
see installation notes on how to create [`audio_device`](#audiodevice-setup)
1. Create a thread for processing downchannel stream in parallel
    ```python
    import threading

    def downchannel_stream_directives():
        for push in a._dc_resp.read_chunked():
            parts = multipart_parse(push, a._dc_resp.headers['content-type'][0].decode())
            a.handle_parts(parts)

    ddt = threading.Thread(target=downstream_directives, name='Downstream Directives Thread')
    ddt.setDaemon(False)
    ddt.start()
    ```

1. Run main loop
    ```python
    while True:
        a.run()
        # and other requests
    ```

### Making Requests
Make speech recognize requests with pre-recorded PCM 16kHz audio file
```python
wav_pcm_s16le_file_like = open('test.wav', 'rb')
a.recognize_speech(wav_pcm_s16le_file_like)
```
Make speech recognize requests with 5 second recording from mic
```python
# from https://gist.github.com/mabdrabo/8678538
import pyaudio
import io


audio = pyaudio.PyAudio()
record_seconds = 5
chunk_size = 1024
rate = 16000

# start Recording
stream = audio.open(format=pyaudio.paInt16, channels=1, rate=rate, input=True, frames_per_buffer=chunk_size)
frames = []
 
for i in range(0, int(rate / chunk_size * record_seconds)):
    data = stream.read(chunk_size)
    frames.append(data)
a.recognize_speech(io.BytesIO(b''.join(frames)))
```
Make speech recognize requests with streaming mic audio
```python
import pyaudio
import threading

mic_stopped = threading.Event()
paudio = pyaudio.PyAudio()

# start Recording
mic_stream = paudio.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=1024)

class StoppableAudioStream:
    def __init__(self, audio, stream):
        self._audio = audio
        self._stream = stream
        self._stopped = False

    def read(self, size=-1):
        if mic_stopped.is_set():
            self._stopped = True
            self._stream.stop_stream()
            self._stream.close()
            self._audio.terminate()
            mic_stopped.clear()
        if self._stopped:
            return b''
        # workaround for pyaudio versions before exception_on_overflow=False
        while True:
            try:
                return self._stream.read(size)
            except:
                logger.exception("exception while reading from pyaudio stream")

a.recognize_speech(StoppableAudioStream(paudio, mic_stream), mic_stopped)
```
## Installation
### External Dependencies
This package depends on common python packages as well as my fork of https://github.com/Lukasa/hyper, which has some changes necessary for simultaneous Tx & Rx
```bash
pip install -r requirements.txt
```
Add necessary audio files for playback during timers and alarms
```bash
alarm.wav
timer.wav
```
### `AudioDevice` Setup
The `AudioDevice` is an abstraction of audio playback capability. The required interface is very simple and can be implemented in many ways.

An implementation using `mplayer`:
```python
import shutil
import subprocess

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
```
An implementation using `afplay`:
```python
import shutil
import subprocess

from audio_player import AudioDevice


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
```
## Test Client
The test client `test.py` is provided which has been tested on macOS and raspbian. It uses https://github.com/Kitt-AI/snowboy for detection of the hotword "Alexa" and `pyaudio` for microphone input.

1. At least one of `mplayer` and `afplay` must be available on the system.
2. The files `tokens.txt` and `secrets.txt` must be present in the working directory. The `tokens.txt` schema is shown in [Notes](#notes); the `secrets.txt` schema is as follows:

    ```json
    {
        "client_id": "my_client_id",
        "client_secret": "my_client_secret"
    }
    ```
3. After installing the [requirements](#external-dependencies), get https://github.com/Kitt-AI/snowboy and follow the general installation instructions and specific ones for swig for Python.

4. `pyaudio` should now be installed; if not please install it. If you install a system package for `pyaudio` and you are using virtualenv and you are unable to import it, see http://stackoverflow.com/questions/3371136/revert-the-no-site-packages-option-with-virtualenv. Now symoblically link the following into the working directory:

    ```bash
    ln -s /path/to/snowboy/resources/ .
    ln -s /path/to/snowboy/examples/Python/snowboydecoder.py .
    ln -s /path/to/snowboy/examples/Python/snowboydetect.py .
    ln -s /path/to/snowboy/examples/Python/_snowboydetect.so .    
    ```

4. run `python test.py`

## Notes
* Tested with Python 3.4
* whenever the `AVS` instance has to refresh the access token, the new access and refresh tokens will be JSON de-serialized to a file named `tokens.txt` in the schema:

    ```json
    {
        "refresh_token": "new_refresh_token",
        "access_token": "new_access_token"
    }
    ```
    * this can be changed in the token refresh write_out method `write_tokens_to_file`
* \* work in progress. error handling, channel interactions, and the interaction model in general still need to be completed to meet AVS guidelines
