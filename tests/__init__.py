"""
Run from the repo root:

    python -m unittest discover -s tests -t . -v

Everything here runs without onnxruntime or a model: tests/fakes.py stands a
SpeechGate on a session that reads probabilities straight off the audio.
test_real_model.py is the exception and skips itself unless the Silero
weights and tests/fixtures/speech_sample.wav (see make_speech_sample.py)
are both present.
"""
