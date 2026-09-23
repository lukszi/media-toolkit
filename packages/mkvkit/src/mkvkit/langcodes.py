"""mkvkit.langcodes -- one language-code table, canonicalised one way.

Three code sets meet in a media file and none of them agrees with the others:

* a speech model emits ISO 639-1 (``en``, ``de``);
* Matroska track headers and most media servers carry ISO 639-2 (``eng``,
  ``deu``) -- and 639-2 has *two* spellings for twenty-odd languages, the
  bibliographic one (``ger``, ``fre``, ``chi``) and the terminological one
  (``deu``, ``fra``, ``zho``);
* BCP-47 tags (``de-DE``, ``pt-BR``) add a region nobody compares consistently.

Everything in this package canonicalises to **ISO 639-2/T** before comparing,
because that is what the track header holds and what a tag write has to
produce. Comparing raw strings is how a track tagged ``ger`` and a detection
of ``de`` become a disagreement that is not one -- and a spurious disagreement
in a language pass is not a cosmetic bug: it proposes a write.

Four codes mean "not a language" and are kept distinct from "unknown":
``und`` (undetermined), ``mis`` (uncoded), ``mul`` (multiple) and ``zxx``
(no linguistic content). Only ``zxx`` is ever a *conclusion*; the other three
are the absence of one.

    >>> canonical("de")
    'deu'
    >>> canonical("GER")
    'deu'
    >>> canonical("pt-BR")
    'por'
    >>> is_unknown("und"), is_unknown("zxx")
    (True, False)
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from types import MappingProxyType
from typing import Final

__all__ = [
    "NO_CONTENT",
    "UNKNOWN_TAGS",
    "canonical",
    "is_unknown",
    "iso1_to_iso2t",
    "same_language",
]

#: ISO 639-1 -> ISO 639-2/T, for every language a general-purpose speech model
#: is likely to emit. Kept as data rather than a dependency: it is a hundred
#: rows that never change, and a package that pulls a library to spell "deu"
#: has made a poor trade.
_ISO1_TO_ISO2T: Final[dict[str, str]] = {
    "aa": "aar", "ab": "abk", "af": "afr", "ak": "aka", "am": "amh",
    "ar": "ara", "as": "asm", "av": "ava", "ay": "aym", "az": "aze",
    "ba": "bak", "be": "bel", "bg": "bul", "bh": "bih", "bi": "bis",
    "bm": "bam", "bn": "ben", "bo": "bod", "br": "bre", "bs": "bos",
    "ca": "cat", "ce": "che", "ch": "cha", "co": "cos", "cr": "cre",
    "cs": "ces", "cu": "chu", "cv": "chv", "cy": "cym",
    "da": "dan", "de": "deu", "dv": "div", "dz": "dzo",
    "ee": "ewe", "el": "ell", "en": "eng", "eo": "epo", "es": "spa",
    "et": "est", "eu": "eus",
    "fa": "fas", "ff": "ful", "fi": "fin", "fj": "fij", "fo": "fao",
    "fr": "fra", "fy": "fry",
    "ga": "gle", "gd": "gla", "gl": "glg", "gn": "grn", "gu": "guj",
    "gv": "glv",
    "ha": "hau", "haw": "haw", "he": "heb", "hi": "hin", "ho": "hmo",
    "hr": "hrv", "ht": "hat", "hu": "hun", "hy": "hye", "hz": "her",
    "ia": "ina", "id": "ind", "ie": "ile", "ig": "ibo", "ii": "iii",
    "ik": "ipk", "io": "ido", "is": "isl", "it": "ita", "iu": "iku",
    "ja": "jpn", "jv": "jav",
    "ka": "kat", "kg": "kon", "ki": "kik", "kj": "kua", "kk": "kaz",
    "kl": "kal", "km": "khm", "kn": "kan", "ko": "kor", "kr": "kau",
    "ks": "kas", "ku": "kur", "kv": "kom", "kw": "cor", "ky": "kir",
    "la": "lat", "lb": "ltz", "lg": "lug", "li": "lim", "ln": "lin",
    "lo": "lao", "lt": "lit", "lu": "lub", "lv": "lav",
    "mg": "mlg", "mh": "mah", "mi": "mri", "mk": "mkd", "ml": "mal",
    "mn": "mon", "mr": "mar", "ms": "msa", "mt": "mlt", "my": "mya",
    "na": "nau", "nb": "nob", "nd": "nde", "ne": "nep", "ng": "ndo",
    "nl": "nld", "nn": "nno", "no": "nor", "nr": "nbl", "nv": "nav",
    "ny": "nya",
    "oc": "oci", "oj": "oji", "om": "orm", "or": "ori", "os": "oss",
    "pa": "pan", "pi": "pli", "pl": "pol", "ps": "pus", "pt": "por",
    "qu": "que",
    "rm": "roh", "rn": "run", "ro": "ron", "ru": "rus", "rw": "kin",
    "sa": "san", "sc": "srd", "sd": "snd", "se": "sme", "sg": "sag",
    "si": "sin", "sk": "slk", "sl": "slv", "sm": "smo", "sn": "sna",
    "so": "som", "sq": "sqi", "sr": "srp", "ss": "ssw", "st": "sot",
    "su": "sun", "sv": "swe", "sw": "swa",
    "ta": "tam", "te": "tel", "tg": "tgk", "th": "tha", "ti": "tir",
    "tk": "tuk", "tl": "tgl", "tn": "tsn", "to": "ton", "tr": "tur",
    "ts": "tso", "tt": "tat", "tw": "twi", "ty": "tah",
    "ug": "uig", "uk": "ukr", "ur": "urd", "uz": "uzb",
    "ve": "ven", "vi": "vie", "vo": "vol",
    "wa": "wln", "wo": "wol",
    "xh": "xho",
    "yi": "yid", "yo": "yor", "yue": "yue",
    "za": "zha", "zh": "zho", "zu": "zul",
}

#: ISO 639-2/B (bibliographic) -> 639-2/T (terminological). Both spellings are
#: legal in a Matroska header and both occur in the wild, sometimes in
#: neighbouring tracks of the same file.
_ISO2B_TO_T: Final[dict[str, str]] = {
    "alb": "sqi", "arm": "hye", "baq": "eus", "bur": "mya", "chi": "zho",
    "cze": "ces", "dut": "nld", "fre": "fra", "geo": "kat", "ger": "deu",
    "gre": "ell", "ice": "isl", "mac": "mkd", "mao": "mri", "may": "msa",
    "per": "fas", "rum": "ron", "slo": "slk", "tib": "bod", "wel": "cym",
}

#: Collapsed spellings that are not aliases in the standard but are in practice:
#: a detector that reports Norwegian Bokmal or Nynorsk is reporting Norwegian,
#: and a track tagged ``nor`` is the only thing it can be compared against.
_COLLAPSE: Final[dict[str, str]] = {"nob": "nor", "nno": "nor"}

#: "We do not know", in the four spellings a file may use. These are the
#: absence of an answer, never an answer.
UNKNOWN_TAGS: Final[frozenset[str]] = frozenset({"", "und", "mis", "mul", "unk", "qaa"})

#: "There is no spoken language here" -- an answer, and a writable tag.
NO_CONTENT: Final[str] = "zxx"

iso1_to_iso2t = MappingProxyType(_ISO1_TO_ISO2T)


def canonical(code: str | None) -> str | None:
    """Canonicalise any of the spellings above to ISO 639-2/T.

    Returns ``None`` for an empty value or one of :data:`UNKNOWN_TAGS`, so a
    caller can write ``if canonical(tag) is None`` instead of remembering
    which four strings mean nothing. An unrecognised code is returned
    lower-cased and unchanged rather than dropped: an unknown language is not
    the same as no language, and silently discarding it would lose evidence.
    """
    if code is None:
        return None
    text = code.strip().lower().replace("_", "-")
    if not text:
        return None
    base = text.split("-", 1)[0]  # a region subtag never changes the language
    if base in UNKNOWN_TAGS:
        return None
    if len(base) == 2:
        base = _ISO1_TO_ISO2T.get(base, base)
    base = _ISO2B_TO_T.get(base, base)
    return _COLLAPSE.get(base, base)


def is_unknown(code: str | None) -> bool:
    """True when the value carries no claim about a language.

    ``zxx`` is deliberately not unknown: a track with no speech in it has been
    decided, and re-deciding it every run is wasted reading.
    """
    return canonical(code) is None


def same_language(left: str | None, right: str | None) -> bool:
    """Compare two codes after canonicalising both. Unknown never matches."""
    a, b = canonical(left), canonical(right)
    return a is not None and a == b
