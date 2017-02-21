CLOSE_TALK = 'CLOSE_TALK'
NEAR_FIELD = 'NEAR_FIELD'
FAR_FIELD = 'FAR_FIELD'

SPEECH_CLOUD_ENDPOINTING_PROFILES = [NEAR_FIELD, FAR_FIELD]


class AudioInputDevice:
    def start_recording(self):
        raise NotImplementedError

    def read(self, size=-1):
        raise NotImplementedError

    def stop_recording(self):
        raise NotImplementedError


