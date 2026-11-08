"""Invite links: a human-readable slug with a TTL that resolves to a PagerAPP contact URI.

The URI format is the one PagerAPP already scans/pastes/NFC-reads:
pagerapp://contact?u1=<inviter's receiving mailbox>&u2=<mailbox for the scanner>
&s=<public server base URL>&n=<display name>&i=<sender id>.
Only the invite link expires; the mailboxes keep their normal retention.
"""

from __future__ import annotations

import random
from urllib.parse import urlencode

_ADJECTIVES = [
    "amber", "brave", "calm", "clever", "crimson", "eager", "gentle", "golden",
    "happy", "jolly", "kind", "lively", "lucky", "mellow", "noble", "polite",
    "proud", "quick", "quiet", "silver", "smooth", "sunny", "swift", "tender",
    "warm", "wise", "bold", "bright", "cool", "fair", "grand", "neat",
]
_ANIMALS = [
    "deer", "fox", "owl", "bear", "wolf", "hare", "lynx", "otter",
    "eagle", "falcon", "heron", "sparrow", "robin", "finch", "wren", "swift",
    "panda", "koala", "tiger", "leopard", "jaguar", "puma", "bison", "moose",
    "elk", "seal", "whale", "dolphin", "shark", "salmon", "trout", "beaver",
]
_NOUNS = [
    "river", "forest", "meadow", "valley", "summit", "island", "harbor", "breeze",
    "cloud", "stone", "ember", "frost", "leaf", "pine", "cedar", "maple",
    "brook", "creek", "dune", "field", "grove", "hill", "lake", "pond",
    "ridge", "shore", "trail", "wood", "glen", "cove", "peak", "vale",
]


def new_slug() -> str:
    """A human-readable, random invite slug like 'calm-deer-river'."""
    return f"{random.choice(_ADJECTIVES)}-{random.choice(_ANIMALS)}-{random.choice(_NOUNS)}"


def contact_uri(u1: str, u2: str | None, s: str | None, n: str | None, i: str | None, scheme: str = "pagerapp") -> str:
    """The exact PagerAPP contact URI (all values percent-encoded UTF-8).

    The scheme prefix is configurable (INVITE_SCHEME, default "pagerapp"), so
    other clients can use their own `<prefix>://` scheme if they wish.
    """
    params: dict[str, str] = {"u1": u1}
    if u2:
        params["u2"] = u2
    if s:
        params["s"] = s
    if n:
        params["n"] = n
    if i:
        params["i"] = i
    return f"{scheme}://contact?" + urlencode(params)
