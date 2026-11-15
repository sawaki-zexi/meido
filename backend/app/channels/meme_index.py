"""Bounded disposable observations, shared by short-lived material adapters.

No pixels, authorization, or business meaning is owned here. Every observation
checks file identity; senders still hash the selected original at dispatch.
"""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import re
import threading
import unicodedata

_GENERIC = frozenset({'今天', '这个', '那个', '这张', '那张', '图片', '表情', '个表', '一下', '什么', '可以', '我们', '你们', '请问', '一个'})

def terms(text):
    text = unicodedata.normalize('NFKC', text).casefold()
    result = set(re.findall(r'[a-z0-9_]+', text))
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        result.update(run[i:i + 2] for i in range(len(run) - 1))
    return frozenset(result) - _GENERIC


def relevance(query, *fields):
    """Exact terms plus Chinese bigrams; popularity never supplies a match."""
    if not query.strip():
        return 1, ''
    query_terms = terms(query)
    searchable = ' '.join(str(field) for field in fields)
    matched = query_terms & terms(searchable)
    if not matched:
        return 0, ''
    return len(matched), '匹配：' + '、'.join(sorted(matched)[:8])


@dataclass(frozen=True)
class ImageFact:
    identity: tuple
    digest: str
    media: str
    size: int
    width: int
    height: int
    format: str
    animated: bool


class MemeIndex:
    def __init__(self):
        self.lock = threading.RLock()
        self.images = OrderedDict()
        self.records = OrderedDict()

    def record(self, key, stamp, load):
        with self.lock:
            cached = self.records.get(key)
            if (not isinstance(cached, tuple) or len(cached) != 2 or cached[0] != stamp
                    or not isinstance(cached[1], dict)):
                self.records.pop(key, None)
                value = load()
                self.records[key] = (stamp, deepcopy(value))
            else:
                value = cached[1]
            self.records.move_to_end(key)
            while len(self.records) > 2048:
                self.records.popitem(last=False)
            return deepcopy(value)

    def image(self, key, stamp, load):
        with self.lock:
            cached = self.images.get(key)
            if (not isinstance(cached, tuple) or len(cached) != 2 or cached[0] != stamp
                    or not isinstance(cached[1], ImageFact)):
                # Failed refresh does not serve an older observation.
                self.images.pop(key, None)
                fact = load()
                self.images[key] = (stamp, fact)
            else:
                fact = cached[1]
            self.images.move_to_end(key)
            while len(self.images) > 2048:
                self.images.popitem(last=False)
            return fact


def shared_index(library):
    with library.app._gate:
        if not isinstance(getattr(library, '_material_index', None), MemeIndex):
            library._material_index = MemeIndex()
        return library._material_index
