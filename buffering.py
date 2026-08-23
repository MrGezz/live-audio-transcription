"""
buffering.py - deciding WHEN a window of audio is ready to transcribe.

Why this exists
---------------
There are two defensible answers to "how do I cut a live stream into Whisper
windows?", and they fail in opposite directions:

  sliding_window          Fixed 4 s windows advancing 2 s at a time. Latency
                          is a constant you can plan around and the backlog
                          is bounded, but a cut lands wherever the clock says
                          - mid-word as often as not. The 2 s of overlap is
                          what hides that, at the price of transcribing every
                          second twice and needing a fuzzy duplicate filter
                          over the result.

  silence_at_end_of_chunk Collect audio, ask the speech gate where the
                          talking stopped, and only cut in the quiet. Nothing
                          is split mid-word and nothing is transcribed twice,
                          so no duplicate filter is needed at all - but
                          through dense speech the cut never comes, and
                          latency is whatever the speaker's next breath says
                          it is.

Neither one wins. Which is right depends on the audio in front of you, so
this module makes it a setting instead of an architecture decision: both
implement one interface, create_strategy() picks between them, and the worker
loop is the same either way.

The mechanism is a pull loop, not a callback:

    strategy.feed(chunk)                    # cheap, decides nothing
    while True:
        d = strategy.next()
        if d.action == "wait":
            break
        ...                                 # transcribe / report the skip

Everything a caller needs in order to explain itself is on the Decision - the
raw level, the gate's verdict, how far behind real time it fell, and a
ready-to-show sentence for the cases where the honest answer is "nothing was
transcribed, and here is why". Nothing here prints, and nothing here throttles
printing: a console wants one line every ten seconds, a web UI wants the
latest one always, and only the caller knows which it is.
"""

import difflib

import numpy as np

from settings import DEFAULTS
from speech_gate import HOP

SAMPLERATE = 16000

# The shortest tail flush() will bother transcribing when a file ends, in
# samples. A quarter second is eight 32 ms VAD frames; below that there is not
# enough left to gate honestly, and Whisper answers a fragment that size with
# invented text more readily than it transcribes it.
MIN_FLUSH_SAMPLES = SAMPLERATE // 4

# How many words of the previous window's tail may go unmatched and a shared
# run still count as the overlap. See _overlap_length, which is the only
# thing that reads it - and the measurement that says 2, where 0 and 1 leave
# duplicates behind and 3 and 4 change nothing.
OVERLAP_SLACK = 2


def _opt(settings, key):
    """
    One option, falling back to settings.py's default when it is absent.

    Deliberately NOT a local default. Repeating the numbers here is exactly
    how the option set came to exist in three copies that disagreed - see the
    settings.py docstring - and a second copy of "buffer is 4" is a second
    place to forget to change it.
    """
    value = settings.get(key)
    return DEFAULTS[key] if value is None else value


def _key(text):
    """
    Comparison key: lowercased, alphanumerics and whitespace only, stripped.

    Whisper punctuates the same words differently either side of an overlap
    ("okay so we" against "Okay, so we..."), which is enough to defeat ==.
    """
    return "".join(c for c in text.lower()
                   if c.isalnum() or c.isspace()).strip()


def _text_of(segment):
    """
    The text out of whatever shape a segment arrives in.

    backend.transcribe() yields (text, language) pairs, but the transcript
    writers and the web UI carry word timings alongside the same text. This
    filter should not be the reason a caller cannot enrich its own results.
    """
    if isinstance(segment, str):
        return segment
    if isinstance(segment, dict):
        return str(segment.get("text", ""))
    if isinstance(segment, (tuple, list)) and segment:
        return str(segment[0])
    return str(getattr(segment, "text", "") or "")


def _words_of(segment):
    """A segment's word timings, in whatever shape it arrived in."""
    if isinstance(segment, dict):
        return list(segment.get("words") or [])
    return list(getattr(segment, "words", None) or [])


def _retext(segment, text, words=None):
    """
    The same segment carrying different text.

    The overlap filter has to be able to remove the first few words of a
    segment rather than only the whole segment, and it has to do that
    without knowing what a segment IS: the backends return Segment, the
    transcript writer and the web UI carry dicts, and live_transcription.py
    still unpacks a bare (text, language) tuple. Anything that carries more
    than its text says so by offering retext(), because only it can decide
    what has to move with the text - Segment's language, its word timings
    and the `translated` flag invariant 16 measures rather than assumes.
    Everything else is rebuilt in the shape it arrived in.
    """
    rewrite = getattr(segment, "retext", None)
    if callable(rewrite):
        return rewrite(text, words)
    if isinstance(segment, str):
        return text
    if isinstance(segment, dict):
        out = dict(segment)
        out["text"] = text
        if words is not None and "words" in out:
            out["words"] = words
        return out
    if isinstance(segment, (tuple, list)) and segment:
        return (text,) + tuple(segment)[1:]
    return text


def _split_words(text):
    """
    A segment's text as display words, each with the key it compares by.

    Split on whitespace and NOT on _key()'s rules, because the trim has to
    put what survives back together afterwards: "dog." is one word to a
    reader and one word to whisper's timings, and only the key it is matched
    on drops the full stop. Keys are therefore allowed to be empty - a
    standalone quote mark is a word to the joiner and nothing to the matcher.
    """
    return [(word, _key(word)) for word in text.split()]


def _drop_leading_words(words, count):
    """
    The word timings left once the first `count` display words are trimmed.

    A token index is not a word index: whisper returns sub-word tokens, so
    "Silero" arrives as "Sil", "er", "ow" and a comma arrives as a token
    belonging to no word at all. What marks a word boundary is the leading
    space the tokenizer keeps on the first token of each word - the same
    thing that makes the tokens concatenate back into the segment's text.
    Counting tokens instead would cut "Silero" into "ow" and leave the word
    timings describing text that is no longer there.
    """
    if count <= 0:
        return list(words or [])
    index, out = -1, []
    for word in words or []:
        text = (word.get("word", "") if isinstance(word, dict)
                else str(getattr(word, "word", "")))
        if index < 0 or text[:1].isspace():
            index += 1
        if index >= count:
            out.append(word)
    return out


def _overlap_length(prev_words, new_words, slack=OVERLAP_SLACK):
    """
    How many of this window's leading words the previous window already said.

    The overlap is at the FRONT of this window and at the BACK of the last
    one, always, because that is what the geometry means. So a run of words
    the two windows share is evidence of the overlap exactly when it reaches
    the previous window's END, and the cut is the furthest such run's far
    edge. Anchoring on the tail is the whole trick, and anchoring on both
    ends is what does not work: whisper re-decodes the overlap differently
    either side of the cut, so difflib routinely splits it into two runs -
    measured at --buffer 4 --slide 1, "a gap only ends" at the head and "it
    has" at the tail, with neither run touching both ends and the trim
    therefore firing on neither.

    `slack` is how many words of the previous window's tail may go unmatched
    and the run still count. It exists because the last word of a window is
    the one the cut went through, so it is the one most likely to come back
    spelled differently. Measured over five geometries of
    tests/fixtures/speech_sample.wav, 0 and 1 both leave duplicates behind
    (49 and 11 repeated words against 10) and 2, 3 and 4 are identical, so
    the useful range saturates at 2.

    A one-word run gets no slack at all, and has to sit in both corners -
    the previous window's last word against this window's first. That
    single exception is worth its line: an interior one-word match is a
    coincidence, and believing it cost three words of real speech in the
    measurement ("...contains speech" against "a gap only in speech" ate
    everything up to the second "speech"), while a corner one-word match is
    the ordinary case of a window opening on the tail of the last word the
    previous one printed ("...long enough" then "enough 400 milliseconds").
    """
    if not prev_words or not new_words:
        return 0
    matcher = difflib.SequenceMatcher(None, prev_words, new_words,
                                      autojunk=False)
    cut = 0
    for a, b, size in matcher.get_matching_blocks():
        if size >= 2:
            if a + size >= len(prev_words) - slack:
                cut = max(cut, b + size)
        elif size == 1 and b == 0 and a + size == len(prev_words):
            cut = max(cut, 1)
    return min(cut, len(new_words))


class Decision(object):
    """
    What next() concluded about the audio it is holding.

    action is one of:
      "wait"        not enough audio yet, or the speech has not finished.
                    Stop calling next() until more audio has been fed.
      "transcribe"  audio is a window to send to the backend.
      "skip"        a window was consumed and deliberately not transcribed;
                    reason says which gate rejected it.
    """

    __slots__ = ("action", "audio", "t_start", "duration", "level", "gate",
                 "reason", "behind", "hint")

    def __init__(self, action, audio=None, t_start=0.0, duration=0.0,
                 level=0.0, gate=None, reason="", behind=0.0, hint=""):
        self.action = action
        self.audio = audio
        self.t_start = t_start      # seconds since session start of audio[0]
        self.duration = duration
        # Mean absolute level of the window, RAW. Deliberately not measured
        # after the peak normalisation the caller applies before inference:
        # quiet room tone measures 0.0064 raw and 0.2214 normalised, which is
        # indistinguishable from speech at 0.2279, and silence_threshold has
        # no other evidence to work with.
        self.level = level
        self.gate = gate            # GateResult, or None when the gate was
        self.reason = reason        # not reached ("silence") or is off
        self.behind = behind        # seconds discarded to catch up
        self.hint = hint

    def __repr__(self):
        return ("<Decision {0} t={1:.2f} dur={2:.2f} level={3:.5f} "
                "reason={4!r} behind={5:.2f}>".format(
                    self.action, self.t_start, self.duration, self.level,
                    self.reason, self.behind))


class BufferingStrategy(object):
    """
    The interface, plus the parts both strategies genuinely share: the sample
    clock, the two-stage gate, and the wording of the skip hints.
    """

    name = ""

    def __init__(self, settings, gate=None):
        self._buf = np.zeros(0, dtype=np.float32)
        self._consumed = 0        # samples ever dropped off the front; the
        self.gate = None          # only thing t_start is derived from
        self.settings = {}
        self.silence_threshold = 0.0
        self.configure(settings, gate)

    # -- audio in ----------------------------------------------------------
    def feed(self, chunk):
        """Add float32 mono 16 kHz samples. Decides nothing."""
        chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if len(chunk):
            self._buf = np.concatenate((self._buf, chunk))

    def pending_seconds(self):
        """Audio held but not yet handed out."""
        return len(self._buf) / float(SAMPLERATE)

    # -- decisions ---------------------------------------------------------
    def next(self):
        """One Decision. Call in a loop until action == "wait"."""
        raise NotImplementedError

    def filter(self, segments):
        """(kept, dropped) for one window's segments."""
        raise NotImplementedError

    # -- lifecycle ---------------------------------------------------------
    def configure(self, settings, gate=None):
        """
        Adopt new settings mid-session.

        Buffered audio survives by default, and subclasses drop it only when
        the change genuinely invalidates it: re-tuning the duplicate filter
        must not cost you the sentence currently being spoken.
        """
        self.settings = dict(settings)
        self.gate = gate
        self.silence_threshold = float(_opt(settings, "silence_threshold"))

    def reset(self):
        """Drop buffered audio. The session clock keeps running."""
        self._discard(len(self._buf))

    def flush(self):
        """
        Hand out whatever is still buffered, short window and all, or None.

        Called once when a finite source ends - a WAV file reaching its last
        sample. Both strategies deliberately hold audio back during normal
        running: a sliding window keeps buffer minus slide seconds as the
        overlap the duplicate filter needs, and a wait-for-silence chunk keeps
        everything until somebody stops talking. Live that is invisible,
        because more audio is always arriving. At the end of a file it is the
        last few seconds of the transcript never appearing at all.

        The gate still applies - a file that ends in silence should not produce
        a final hallucinated caption - so this can legitimately return a skip
        or None.
        """
        if len(self._buf) < MIN_FLUSH_SAMPLES:
            # Shorter than one VAD frame plus a little. Whisper answers a
            # fragment this size with invented text far more readily than it
            # transcribes it, which is a poor way to end a transcript.
            return None
        audio = self._buf.copy()
        t_start = self._consumed / float(SAMPLERATE)
        duration = len(audio) / float(SAMPLERATE)
        level = float(np.mean(np.abs(audio)))
        self._discard(len(self._buf))
        reason, result, hint = self._screen(audio, level)
        if reason is not None:
            return Decision("skip", t_start=t_start, duration=duration,
                            level=level, gate=result, reason=reason, hint=hint)
        return Decision("transcribe", audio=audio, t_start=t_start,
                        duration=duration, level=level, gate=result)

    # -- shared internals --------------------------------------------------
    def _discard(self, n):
        """Drop n samples off the front, keeping t_start honest."""
        if n > 0:
            self._consumed += n
            self._buf = self._buf[n:]

    def _wait(self):
        return Decision("wait", t_start=self._consumed / float(SAMPLERATE),
                        duration=self.pending_seconds())

    def _screen(self, audio, level):
        """
        The two-stage gate, cheapest test first -> (reason, gate, hint).

        The level check rejects true silence for free; the VAD then rejects
        audio that is loud but not speech - fans, music, keyboards, room tone
        - which is exactly what Whisper answers with invented text. Both
        reject before any inference is paid for, so a quiet room costs
        nothing instead of a full pass plus a hallucinated caption.

        analyze() rather than has_speech() only so the hint can say how far
        short the buffer fell. It is the same single inference pass, and "6 of
        the 8 frames needed" tells you what to set the frame requirement to,
        where a bare "no speech detected" leaves you guessing.
        """
        if level < self.silence_threshold:
            return "silence", None, self._hint_silence(level)
        if self.gate is None:
            return None, None, ""
        result = self.gate.analyze(audio)
        if result.has_speech:
            return None, result, ""
        return "no-speech", result, self._hint_no_speech(level, result)

    def _hint_silence(self, level):
        return ("Level {0:.5f} is below the silence threshold {1} - this is "
                "silence, not quiet speech. If audio IS playing: check you "
                "picked the output device you are actually listening on, "
                "raise the Windows volume, or lower the silence level "
                "(--silence-threshold).".format(level, self.silence_threshold))

    def _hint_no_speech(self, level, result):
        if result.frames == 0:
            # Nothing in the window cleared the probability threshold at all,
            # so asking for fewer frames CANNOT help - there are none to ask
            # for. Instrumental passages and steady noise land here.
            return ("Audible (level {0:.5f}) but Silero found no speech at "
                    "all. Loosening the speech-needed setting "
                    "(--vad-min-speech-ms) cannot help, because there is no "
                    "frame to count. Turn the speech gate off (--no-vad) to "
                    "transcribe it anyway - instrumentals, heavily processed "
                    "vocals - or lower the speech probability "
                    "(--vad-threshold) if you believe there is quiet speech "
                    "in there.".format(level))
        return ("Audible (level {0:.5f}) but only {1} of the {2} speech "
                "frames needed. Sung vocals score far below speech; try a "
                "speech-needed of {3} ms (--vad-min-speech-ms) - not the "
                "probability threshold, which lets room tone in at about the "
                "same point it lets music in. Turning the gate off (--no-vad) "
                "skips the check entirely.".format(
                    level, result.frames, result.min_frames,
                    int(result.frames * 32)))

    def _hint_word_starting(self, level, tail_s):
        return ("Audible (level {0:.5f}) but the only speech is the last "
                "{1:.0f} ms - a word starting as the buffer ended. It stays "
                "buffered and opens the next chunk."
                .format(level, tail_s * 1000))


class SlidingWindow(BufferingStrategy):
    """
    Fixed windows of `buffer` seconds advancing `slide` seconds per pass, so
    consecutive windows share buffer-slide seconds of audio. That overlap is
    what keeps a word split by one cut intact in the neighbouring window, and
    filter() is what stops it being printed twice.
    """

    name = "sliding_window"

    def __init__(self, settings, gate=None):
        self._prev_keys = []
        BufferingStrategy.__init__(self, settings, gate)

    def configure(self, settings, gate=None):
        was = (getattr(self, "buffer_s", None), getattr(self, "slide_s", None))
        BufferingStrategy.configure(self, settings, gate)
        # Floored at one second each. settings.validate() already enforces
        # that, but next() is a pull loop the caller spins until it answers
        # "wait": a slide of 0 discards nothing, so the same window is handed
        # out forever and the worker thread never returns - 9873 identical
        # decisions in one second, measured. A geometry that cannot advance
        # has to become a slow one, not a hang.
        self.buffer_s = max(1, int(_opt(settings, "buffer")))
        self.slide_s = max(1, int(_opt(settings, "slide")))
        self.dedup_threshold = float(_opt(settings, "dedup_threshold"))
        if was[0] is not None and was != (self.buffer_s, self.slide_s):
            # The window geometry changed, so audio already accumulated
            # belongs to a window that will never exist: carrying it forward
            # hands out one pass that is neither the old length nor aligned to
            # the new slide. The dedup history is NOT dropped with it - the
            # text of the last window is still the text of the last window,
            # and forgetting it lets the first window after the change reprint
            # a sentence that was just shown.
            self._discard(len(self._buf))
        # Note what is deliberately missing from that test: dedup_threshold
        # and silence_threshold change nothing about the audio, so re-tuning
        # them mid-sentence costs no audio.

    def next(self):
        window = SAMPLERATE * self.buffer_s
        if len(self._buf) < window:
            return self._wait()

        behind, hint = 0.0, ""
        # Real-time catch-up, BEFORE transcribing. The caller drains its whole
        # queue into feed() before asking, so trimming after the window is
        # handed out never bounds what the backend actually sees: each pass
        # would get `buffer` seconds plus everything captured during the
        # previous (slower) call, which feeds back on itself and grows without
        # limit once the GPU stops keeping up.
        max_len = SAMPLERATE * (self.buffer_s + 2 * self.slide_s)
        if len(self._buf) > max_len:
            behind = (len(self._buf) - window) / float(SAMPLERATE)
            self._discard(len(self._buf) - window)
            hint = ("{0:.1f}s behind real time - skipped ahead to stay live. "
                    "Inference is slower than the slide: use a smaller or "
                    "more quantized model, or raise the slide."
                    .format(behind))

        # Copied because the tail of this window is still buffered as the
        # front of the next one: a caller that normalises in place would
        # rewrite audio that has not been handed out yet.
        audio = self._buf.copy()
        t_start = self._consumed / float(SAMPLERATE)
        duration = len(audio) / float(SAMPLERATE)
        level = float(np.mean(np.abs(audio)))
        reason, result, skip_hint = self._screen(audio, level)
        hint = " ".join(part for part in (hint, skip_hint) if part)

        # Slide AFTER the window is handed out, so consecutive windows overlap
        # instead of abutting.
        self._discard(min(SAMPLERATE * self.slide_s, len(self._buf)))

        if reason is not None:
            return Decision("skip", t_start=t_start, duration=duration,
                            level=level, gate=result, reason=reason,
                            behind=behind, hint=hint)
        return Decision("transcribe", audio=audio, t_start=t_start,
                        duration=duration, level=level, gate=result,
                        behind=behind, hint=hint)

    def filter(self, segments):
        """
        Remove the speech this window shares with the previous one.

        The trim is per WORD, and it has to be, because the unit a whisper
        server returns is not the unit the overlap arrives in. Segments are
        cut where the server's own caption wrapping says, which has no
        relationship at all to where the audio was cut: measured against a
        live whisper-server over the repo's own fixture, window 2 of
        --buffer 4 --slide 2 came back as the single segment "Silerow
        decides whether this buffer contains speech.", of which the first
        four words were already printed by window 1. A keep-or-drop test
        over whole segments has no move that expresses "print the second
        half of this", so it printed the lot.

        It could not even catch the segments that ARE whole repeats. Window
        1's "over the lazy dog." against window 0's "the quick brown fox
        jumps over the lazy dog." scores 0.57 on difflib's ratio, because
        the ratio is over the sum of both lengths and the previous line is
        the longer one; the shipped threshold is 0.80. Over twelve windows
        of that fixture the old filter fired ZERO times, and the transcript
        came back 98 words long where 56 were spoken.

        Three things are load-bearing here, and all three were learned the
        hard way:

          * Only the IMMEDIATELY previous window can overlap this one, so
            matching against a longer history just eats deliberate repeats:
            "Thank you." either side of an intervening window scores 1.00
            against itself and would silently vanish at any threshold.
          * The remembered key list is what the window DECODED, not what it
            printed. A window's own trimmed-off head was still spoken inside
            it, so the next window overlaps that too; remembering only the
            printed part lets the same line reappear one pass later.
          * The whole-segment test below still runs, on what survives the
            trim. It fired on nothing in any of the five geometries measured
            - the trim gets there first - so it costs nothing, and it is the
            only thing `--dedup-threshold` reaches. Deleting it as dead would
            leave a shipped setting that changes nothing.

        Measured over five geometries of tests/fixtures/speech_sample.wav
        (56 words spoken), words printed / duplicated / lost:

            buffer/slide   before          after
            4 / 2          98 / 32 / 0     59 /  3 / 0
            4 / 1         146 / 63 / 0     63 /  4 / 0
            6 / 2         128 / 60 / 0     58 /  2 / 0
            3 / 2          74 / 15 / 0     57 /  3 / 0
            8 / 4          67 / 17 / 5     51 /  1 / 5

        The five lost words at 8/4 are lost before the filter sees them -
        the old filter loses the same five - and no geometry loses a word to
        the trim itself.
        """
        keys = [_key(_text_of(segment)) for segment in segments]
        per_segment = [_split_words(_text_of(segment)) for segment in segments]
        flat = [pair for words in per_segment for pair in words]
        # Punctuation-only words cannot be matched, so they are not offered
        # to the matcher - and the answer therefore has to be mapped back
        # onto the words that will actually be printed.
        matchable = [i for i, (_, key) in enumerate(flat) if key]
        cut = _overlap_length(" ".join(self._prev_keys).split(),
                              [flat[i][1] for i in matchable])
        end = matchable[cut] if cut < len(matchable) else len(flat)

        kept, dropped, offset = [], [], 0
        head_text = " ".join(word for word, _ in flat[:end])
        if _key(head_text):
            # Reported as a caption in its own right, because that is what it
            # would have been. A plain string, not a segment: a dropped item
            # is only ever read for its text, and the timings that made this
            # one part of a Segment now belong to the caption it was cut off.
            dropped.append(head_text)
        for index, segment in enumerate(segments):
            words = per_segment[index]
            head = max(0, min(end - offset, len(words)))
            offset += len(words)
            body = words[head:]
            if not body:
                dropped.append(segment)
                continue
            text = " ".join(word for word, _ in body)
            if head:
                segment = _retext(
                    segment, text,
                    _drop_leading_words(_words_of(segment), head))
            key = _key(text)
            if key and not self._is_duplicate(key):
                kept.append(segment)
            else:
                # Empty keys land here too: a segment left holding only
                # punctuation has nothing to print and nothing to compare.
                dropped.append(segment)
        # What was DECODED, before the trim - see the second bullet above.
        self._prev_keys = keys
        return kept, dropped

    def _is_duplicate(self, key):
        return any(difflib.SequenceMatcher(None, key, prev).ratio()
                   > self.dedup_threshold for prev in self._prev_keys)

    def reset(self):
        """
        Drop the buffered audio AND the text that described it.

        Deliberately unlike configure(), which keeps the dedup history on
        purpose: re-tuning a setting mid-sentence leaves the previous window
        still adjacent to the next one, so its words are still the overlap.
        reset() throws the audio away, and after that the next window is not
        adjacent to anything - so matching against the old text can only trim
        words nobody said twice. Cheap when the filter could merely drop a
        whole segment scoring over 0.80; a word-level trim will happily eat
        a leading phrase that coincides.
        """
        BufferingStrategy.reset(self)
        self._prev_keys = []


class SilenceAtEndOfChunk(BufferingStrategy):
    """
    Wait for the speech gate to say the talking stopped, then cut in the gap.

    Adapted from the strategy of the same name in VoiceStreamAI (MIT,
    Alessandro Saccoia), re-based on our ONNX gate instead of its pyannote
    pipeline. Three deliberate departures from the original, all marked
    below: this one does not leak silence, it cannot wait forever, and it
    does not throw the start of a word away with the quiet in front of it.
    """

    name = "silence_at_end_of_chunk"

    def __init__(self, settings, gate=None):
        self._retry_at = 0
        self._cut_warned = False
        BufferingStrategy.__init__(self, settings, gate)

    def configure(self, settings, gate=None):
        BufferingStrategy.configure(self, settings, gate)
        self.chunk_length = float(_opt(settings, "chunk_length"))
        self.chunk_offset = float(_opt(settings, "chunk_offset"))
        self.chunk_max_length = float(_opt(settings, "chunk_max_length"))
        # Pending audio is never dropped here, unlike the sliding window:
        # this strategy has no fixed window length for it to be the wrong size
        # for. A longer chunk_length just means keep listening, and what is
        # already buffered is exactly the audio the new setting would have
        # collected anyway. Only the retry gate is reset, so a shortened
        # offset takes effect on the next call instead of one offset later.
        self._retry_at = 0

    def next(self):
        need = max(1, int(round(self.chunk_length * SAMPLERATE)))
        # Never look at, or hand out, more than one force-cut's worth in a
        # single pass. len(self._buf) is NOT bounded by chunk_max_length: one
        # feed() can carry any amount at all - a whole WAV arrives in a couple
        # of blocks under --file-fast, and everything captured while the
        # worker was paused for a benchmark lands in one drain. Unclamped, a
        # 67.7 s drain came back as a single 67.7 s window: one gate pass and
        # one inference over eight times the length chunk_max_length promises.
        # Clamped, the surplus stays buffered and the caller's pull loop takes
        # it a chunk at a time. max() because a chunk_max_length below
        # chunk_length would otherwise hold the buffer back forever.
        limit = max(need, int(round(self.chunk_max_length * SAMPLERATE)))
        if self.gate is None:
            # Degrading to a fixed chunker (see below) means cutting at
            # chunk_length, not at the force-cut. Clamped here rather than at
            # the cut so the level below measures the audio actually sent.
            limit = need
        held = min(len(self._buf), limit)
        if held < need:
            return self._wait()

        forced = held >= limit
        # Re-running the gate on every fed chunk would re-analyse the entire
        # pending buffer ~8 times a second for one new frame of evidence.
        # The original's retry_after_bytes idea, kept: once the answer is
        # "still talking", nothing can change it until at least one offset of
        # new audio exists. The force-cut is tested first so it can never be
        # delayed by this.
        if not forced and held < self._retry_at:
            return self._wait()

        audio = self._buf[:held].copy()
        t_start = self._consumed / float(SAMPLERATE)
        duration = held / float(SAMPLERATE)
        level = float(np.mean(np.abs(audio)))

        if level < self.silence_threshold:
            self._take(held)
            return Decision("skip", t_start=t_start, duration=duration,
                            level=level, reason="silence",
                            hint=self._hint_silence(level))

        if self.gate is None:
            # With no VAD there is nothing to locate the pause with, so this
            # degrades to a plain fixed chunker at chunk_length rather than
            # silently never cutting at all. `held` is chunk_length exactly.
            self._take(held)
            return Decision("transcribe", audio=audio, t_start=t_start,
                            duration=duration, level=level)

        result = self.gate.analyze(audio)      # exactly one inference pass
        if not result.has_speech:
            # The original returns early here and KEEPS the audio. That
            # leaks: an unattended silent stream grows the pending buffer
            # without limit, and every later pass re-analyses more of it.
            # Nothing arriving later can turn this audio into speech, so drop
            # it - the caller is told why.
            #
            # With one exception: a run still open at the buffer end. Its
            # frames are under min_frames because the buffer ended inside it,
            # not because it is a click - a word has just started, and
            # dropping it with the quiet opens the next chunk mid-word, the
            # cut this whole strategy exists to avoid. So drop only the quiet
            # in front of it and keep the run. The leak guard holds: the kept
            # tail is bounded by _open_run_start at min_frames, so a silent
            # stream keeps at most one word-start per pass, and the next pass
            # closes it and drops it as the click it turned out to be.
            start = self._open_run_start(result, held)
            if start == 0 and not forced:
                # The buffer IS the word start - chunk_length is shorter than
                # min_speech_ms. Still talking, then: wait for more, exactly
                # as an open run that passed has_speech does below.
                self._retry_at = held + int(round(self.chunk_offset
                                                  * SAMPLERATE))
                return self._wait()
            if not start:                   # None, or 0 under the force-cut
                self._take(held)
                return Decision("skip", t_start=t_start, duration=duration,
                                level=level, gate=result, reason="no-speech",
                                hint=self._hint_no_speech(level, result))
            self._take(start)
            quiet = audio[:start]
            return Decision("skip", t_start=t_start,
                            duration=start / float(SAMPLERATE),
                            level=float(np.mean(np.abs(quiet))), gate=result,
                            reason="no-speech",
                            hint=self._hint_word_starting(
                                level, (held - start) / float(SAMPLERATE)))

        end = result.last_speech_end
        finished = end is not None and end < duration - self.chunk_offset
        if finished or forced:
            hint = ""
            if forced and not finished:
                # The original has no upper bound at all: through continuous
                # speech its pending buffer grows forever and no caption is
                # ever emitted. A word split by a cut is a worse transcript;
                # no transcript is not a transcript.
                cut = self._pause_cut(result, held)
                if cut is None:
                    hint = ("No pause found in {0:.1f}s of continuous "
                            "speech - cutting anyway so captions keep "
                            "coming, which may split a word. Raise the "
                            "force-cut length to wait longer, or raise the "
                            "trailing silence if this speaker's pauses are "
                            "short.".format(duration))
                else:
                    hint = ("No pause long enough to end the chunk in "
                            "{0:.1f}s - force-cut pulled back to {1:.2f}s, "
                            "the middle of the longest gap in it, so it "
                            "lands in quiet instead of mid-word. The other "
                            "{2:.2f}s stays buffered as the start of the "
                            "next chunk."
                            .format(duration, cut / float(SAMPLERATE),
                                    (held - cut) / float(SAMPLERATE)))
                    # Everything the Decision reports has to describe the audio
                    # actually handed out, not the buffer that was analysed:
                    # t_start comes from _consumed and the transcript's
                    # subtitle timecodes are t_start plus word offsets, so a
                    # duration that overstates the window by the discarded
                    # tail shifts every later caption by that much.
                    held = cut
                    audio = audio[:held]
                    duration = held / float(SAMPLERATE)
                    level = float(np.mean(np.abs(audio)))
            self._take(held)
            return Decision("transcribe", audio=audio, t_start=t_start,
                            duration=duration, level=level, gate=result,
                            hint=hint)

        # Still talking. Keep everything and wait for the pause.
        self._retry_at = held + int(round(self.chunk_offset * SAMPLERATE))
        return self._wait()

    def filter(self, segments):
        """
        Pass-through, on purpose.

        These windows are cut end to end in the gaps and never overlap, so
        there is no repeated audio for a duplicate filter to find. Running one
        anyway could only remove genuine repetition - a speaker actually
        saying "no, no" across two chunks - which is not a bug to fix.
        """
        return list(segments), []

    def reset(self):
        """
        As the base class, plus disarming the retry gate.

        Without this, reset() leaves _retry_at holding a sample count from a
        buffer that no longer exists - measured at 8.40 s against an emptied
        buffer - and the next chunk sits in "still talking" until the refilled
        buffer passes that mark or the force-cut fires, whichever comes first.
        """
        self._take(len(self._buf))

    def _take(self, n):
        self._discard(n)
        self._retry_at = 0

    @staticmethod
    def _open_run_start(result, held):
        """
        Samples of quiet in front of a word starting at the buffer end, or
        None when the buffer does not end inside a SHORT open run.

        Short is stated on the run's own length, not on result.frames: that
        count is of frames over the threshold, which an open run undercounts
        while it idles in the hysteresis band, and a 20-frame run scoring
        one such frame is "audible but not speech", not a word starting. At
        most min_frames - the same limit a closed run is dropped under -
        plus the partial frame a buffer that is not a whole number of frames
        long ends on.
        """
        if not result.segments:
            return None
        start_s, end_s = result.segments[-1]
        if end_s != result.duration:
            return None                 # closed: judged on its full length
        start = int(round(start_s * SAMPLERATE))
        if held - start >= (result.min_frames + 1) * HOP:
            return None
        return start

    def _pause_cut(self, result, held):
        """
        Where to force-cut `held` samples so the cut is in quiet -> samples.

        The force-cut used to land wherever the clock said. Measured on a
        49.3 s sample, the mean absolute level AT the cut was 0.0656 to
        0.0756 - squarely inside a loud word, so half of it goes to this
        chunk and half to the next and Whisper guesses at both. Cutting at
        the middle of the longest pause instead measures 0.0002 to 0.0018,
        and on that sample at a force-cut of 8 s all six placed cuts land on
        exactly 0.0000 across 50 ms either side.

        This is the change faster-whisper made at max_speech_duration_s
        ("Adds new VAD parameters", #1386): not the LAST silence before the
        limit, which is as likely as not to be a between-word gap, but the
        LONGEST one anywhere in the buffer.

        None when there is no qualifying pause at all - a genuinely continuous
        stretch - and the caller then falls back to the blunt cut, because a
        cut in the wrong place still beats no caption.

        limit_s is the length of what was analysed rather than
        chunk_max_length, because next() clamps the two together: a
        chunk_max_length below chunk_length would otherwise ask for a cut past
        the end of the audio this GateResult was built from.
        """
        try:
            cut = result.best_cut(limit_s=held / float(SAMPLERATE))
        except Exception as e:
            # Placement is an improvement on the force-cut, not a requirement
            # of it, and this runs on the worker thread outside the try/except
            # that guards transcribe(): a gate too old to know where the
            # pauses are must cost the placement, not the captions. Said once
            # - it would otherwise repeat on every force-cut for the whole
            # session.
            if not self._cut_warned:
                self._cut_warned = True
                print("[chunking] cannot place the force-cut ({0}) - cutting "
                      "at the limit instead.".format(e))
            return None
        if cut is None:
            return None
        samples = int(round(cut * SAMPLERATE))
        # A midpoint rounding to 0, or to the whole buffer, is not a cut: the
        # first hands Whisper an empty window and the second keeps nothing
        # back, and either way this is just the blunt cut with extra steps.
        if samples < 1 or samples >= held:
            return None
        # A pull-back shorter than flush()'s own floor trades a bad cut for a
        # bad window, and it is reachable in range: at --vad-min-speech-ms 60
        # - the value settings.py suggests for singing - a 0.07 s burst before
        # a 0.25 s pause pulls an 8.00 s force-cut back to 0.208 s, and at 32
        # down to 0.144 s. whisper-server answers that 0.144 s window with
        # "[BLANK_AUDIO]", which nothing downstream filters, where the 8.00 s
        # window it replaced transcribes the sentence correctly. Cutting
        # bluntly instead costs the placement and leaves the burst inside the
        # window it was already in before this existed.
        if samples < MIN_FLUSH_SAMPLES:
            return None
        return samples


STRATEGIES = {
    SlidingWindow.name: SlidingWindow,
    SilenceAtEndOfChunk.name: SilenceAtEndOfChunk,
}


def create_strategy(settings, gate=None):
    """
    The strategy named by settings["strategy"], falling back to the sliding
    window when the name is not one we have.
    """
    name = _opt(settings, "strategy")
    factory = STRATEGIES.get(name)
    if factory is None:
        print("[chunking] unknown strategy '{0}' - using {1}. Known: "
              "{2}".format(name, SlidingWindow.name,
                           ", ".join(sorted(STRATEGIES))))
        factory = SlidingWindow
    return factory(settings, gate)
