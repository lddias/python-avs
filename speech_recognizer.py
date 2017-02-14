class AudioInputDevice:
    def start_recording(self):
        raise NotImplementedError

    def read(self, size=-1):
        raise NotImplementedError

    def stop_recording(self):
        raise NotImplementedError