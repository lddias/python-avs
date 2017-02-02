import time


def fake_mic(logger, q, mic_stopped):
    time.sleep(60)
    logger.debug("TRIGGERED")

    class StoppableAudioStreamLike:
        def __init__(self, file):
            self._f = file
            self._eof = False
            self._last_byte = None

        def read(self, size=-1):
            if mic_stopped.is_set():
                logger.info("MIC STOP REQUESTED")
                mic_stopped.clear()
                return b''
            if self._eof:
                ret = self._last_byte
            else:
                ret = self._f.read(size)
            if len(ret) < size:
                self._last_byte = ret[-1:]
                self._eof = True
                ret += ret[-1:] * (size - len(ret))
            assert len(ret) == size
            return ret

    q.put(('hotword', StoppableAudioStreamLike(open('flashbriefing2.wav', 'rb')), mic_stopped))


def fake_mic2(logger, q, mic_stopped):
    time.sleep(3)
    logger.debug("TRIGGERED")
    q.put(('hotword', open('timer.wav', 'rb'), None))
