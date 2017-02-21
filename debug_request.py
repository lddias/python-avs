import sys

import pyaudio
import wave
from requests_toolbelt import MultipartDecoder


filename = sys.argv[1]
boundary = open(filename, 'rb').readline()[2:-2].decode()
decoder = MultipartDecoder(open(filename, 'rb').read(), 'multipart/form-data; boundary={}'.format(boundary), 'latin1')
parts = [part for part in decoder.parts]
print(len(parts))
print(parts[0].content)
print(parts[1].content[:100])
paudio = pyaudio.PyAudio()

waveFile = wave.open('{}.wav'.format(filename), 'wb')
waveFile.setnchannels(1)
waveFile.setsampwidth(paudio.get_sample_size(pyaudio.paInt16))
waveFile.setframerate(16000)
waveFile.writeframes(parts[1].content)
waveFile.close()
