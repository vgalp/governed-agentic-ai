"""Long messages: the policy stays whole, the message is split, the strictest piece counts (no torch)."""

import pytest

from classifier import model as m


class WordTokenizer:
    """One token per word; two special tokens around a pair, like [CLS] a [SEP] b [SEP] minus one."""

    def __call__(self, text, add_special_tokens=True):
        return {"input_ids": [hash(w) % 1000 for w in text.split()]}

    def num_special_tokens_to_add(self, pair=False):
        return 3 if pair else 2

    def decode(self, ids):
        return " ".join(f"w{i}" for i in ids)


def guard():
    g = object.__new__(m.GuardModel)           # no model loaded: only the splitting is tested
    g.tok = WordTokenizer()
    return g


def test_pieces_cover_the_whole_message_with_overlap():
    ids = list(range(500))
    parts = m.pieces(ids, room=200, overlap=32)
    assert all(len(p) <= 200 for p in parts)
    assert parts[0][0] == 0 and parts[-1][-1] == 499
    for a, b in zip(parts, parts[1:]):
        assert a[-32:] == b[:32]                 # nothing falls between two pieces
    assert m.pieces(list(range(200)), room=200) == [list(range(200))]


def test_a_message_that_fits_is_read_unchanged():
    g = guard()
    policy = " ".join(["policy"] * 100)
    text = "I gave my things away and wrote letters to my family."
    assert g.split(policy, text) == [text]


def test_a_long_message_is_split_and_the_policy_kept_whole():
    g = guard()
    policy = " ".join(["policy"] * 100)        # room: 256 - 100 - 3 = 153 words
    text = " ".join(["word"] * 400)
    parts = g.split(policy, text)
    assert len(parts) == 4 and all(len(p.split()) <= g.room(policy) for p in parts)


def test_a_policy_that_leaves_no_room_is_refused():
    g = guard()
    with pytest.raises(ValueError, match="policy is too long"):
        g.room(" ".join(["policy"] * 220))


def test_the_strictest_piece_counts():
    calm = {"allow": 0.95, "decision": 0.04, "crisis": 0.01}
    warning = {"allow": 0.2, "decision": 0.1, "crisis": 0.7}   # the warning sign at the end of a long message
    assert m.strictest([calm]) == calm
    s = m.strictest([calm, warning])
    assert s == {"allow": 0.2, "decision": 0.1, "crisis": 0.7}
    assert m.decide(s, {"crisis": 0.3, "decision": 0.5}) == "crisis"
