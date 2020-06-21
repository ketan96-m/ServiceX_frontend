from hashlib import blake2b
from pathlib import Path
import re
import tempfile
from typing import AsyncIterator, Callable, Optional, Dict, List, Tuple, Any

from tqdm.auto import tqdm

from lark import Transformer, Token
from qastle import parse


# Where shall we store files by default when we pull them down?
default_file_cache_name = Path(tempfile.gettempdir()) / 'servicex'


class ServiceXException(Exception):
    'Raised when something has gone wrong in the ServiceX remote service'
    def __init__(self, msg):
        super().__init__(self, msg)


class ServiceXUnknownRequestID(Exception):
    'Raised when we try to access ServiceX with a request ID it does not know about'
    def __init__(self, msg):
        super().__init__(self, msg)


class ServiceXFailedFileTransform(Exception):
    'Raised when a file(s) fail to transform'
    def __init__(self, msg):
        super().__init__(self, msg)


def sanitize_filename(fname: str):
    'No matter the string given, make it an acceptable filename'
    return fname.replace('*', '_') \
                .replace(';', '_') \
                .replace(':', '_')


StatusUpdateCallback = Callable[[Optional[int], int, int, int], None]
# The sig of the call-back

StatusUpdateFactory = Callable[[str], StatusUpdateCallback]
# Factory method that returns a factory that can run the status callback
# First argument is a string.


class _status_update_wrapper:
    '''
    Internal class to make it easier to deal with the updater
    '''
    def __init__(self, callback:
                 Optional[StatusUpdateCallback] = None):
        self._callback = callback
        self.reset()

    def reset(self):
        'Reset back to zero'
        self._total: Optional[int] = None
        self._processed: Optional[int] = None
        self._downloaded: Optional[int] = None
        self._failed: int = 0
        self._remaining: Optional[int] = None

    @property
    def total(self) -> Optional[int]:
        return self._total

    @property
    def failed(self) -> int:
        return self._failed

    @property
    def downloaded(self) -> Optional[int]:
        return self._downloaded

    def broadcast(self):
        'Send an update back to the system'
        if self._callback is not None:
            processed = self._processed if self._processed is not None else 0
            downloaded = self._downloaded if self._downloaded is not None else 0
            failed = self._failed if self._failed is not None else 0
            self._callback(self._total, processed, downloaded, failed)

    def _update_total(self):
        'Update total number of files, without letting inconsistencies change things'
        if self._processed is not None \
                and self._remaining is not None \
                and self._failed is not None:
            total = self._remaining + self._processed + self._failed
            if self._total is None:
                self._total = total
            else:
                if self._total < total:
                    self._total = total

    def update(self, processed: Optional[int] = None,
               downloaded: Optional[int] = None,
               remaining: Optional[int] = None,
               failed: Optional[int] = None):
        if processed is not None:
            self._processed = processed
        if downloaded is not None:
            self._downloaded = downloaded
        if failed is not None:
            self._failed = failed
        if remaining is not None:
            self._remaining = remaining
        self._update_total()

    def inc(self, downloaded: Optional[int] = None):
        if downloaded is not None:
            if self._downloaded is None:
                self._downloaded = downloaded
            else:
                self._downloaded += downloaded


TransformTuple = Tuple[Optional[int], int, Optional[int]]


# TODO: Rename to user feedback
async def stream_transform_updates(stream: AsyncIterator[TransformTuple],
                                   notifier: _status_update_wrapper):
    '''
    As the transformed status goes by, update the notifier with the new
    values.
    '''
    async for p in stream:
        remaining, processed, failed = p
        notifier.update(processed=processed, failed=failed, remaining=remaining)
        notifier.broadcast()
        yield p


def _run_default_wrapper(ds_name: str) -> StatusUpdateCallback:
    '''
    Create a feedback object for everyone to use to pass feedback to. Uses tqdm (default).
    '''
    return _default_wrapper_mgr(ds_name).update


def _null_progress_feedback(ds_name: str) -> None:
    '''
    Internal routine to create a feedback object that does not
    give anyone feedback!
    '''
    return None


class _default_wrapper_mgr:
    'Default progress bar'
    def __init__(self, sample_name: Optional[str] = None):
        self._tqdm_p: Optional[tqdm] = None
        self._tqdm_d: Optional[tqdm] = None
        self._sample_name = sample_name

    def _init_tqdm(self):
        if self._tqdm_p is not None:
            return

        self._tqdm_p = tqdm(total=9e9, desc=self._sample_name, unit='file',
                            leave=True, dynamic_ncols=True,
                            bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}]')
        self._tqdm_d = tqdm(total=9e9, desc="        Downloaded", unit='file',
                            leave=True, dynamic_ncols=True,
                            bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}]')

    def _update_bar(self, bar: Optional[tqdm], total: Optional[int], num: int, failed: int):
        assert bar is not None, 'Internal error - bar was not initalized'
        if total is not None:
            if bar.total != total:
                # There is a bug in the tqm library if running in a notebook
                # See https://github.com/tqdm/tqdm/issues/688
                # This forces us to do the reset, sadly.
                old_num = bar.n  # type: int
                bar.reset(total)
                bar.update(old_num)
        bar.update(num - bar.n)
        bar.refresh()

        if total is not None and (num + failed) == total:
            bar.close()

    def update(self, total: Optional[int], processed: int, downloaded: int, failed: int):
        self._init_tqdm()
        self._update_bar(self._tqdm_p, total, processed, failed)
        self._update_bar(self._tqdm_d, total, downloaded, failed)


# Changes in the json that won't affect the result
_json_keys_to_ignore_for_hash = ['workers']


def _query_cache_hash(json_query: Dict[str, str]) -> str:
    '''
    Return a query cache file.
    '''
    hasher = blake2b(digest_size=20)
    for k, v in json_query.items():
        if k not in _json_keys_to_ignore_for_hash:
            if k == 'selection':
                v = clean_linq(v)
            hasher.update(k.encode())
            hasher.update(str(v).encode())
    hash = hasher.hexdigest()
    return hash


def _string_hash(s_list: List[str]) -> str:
    '''
    Return a hash for an input list of strings.
    '''
    hasher = blake2b(digest_size=20)
    for v in s_list:
        hasher.update(v.encode())
    hash = hasher.hexdigest()
    return hash


def clean_linq(q: str) -> str:
    '''
    Normalize the variables in a qastle expression. Should make the
    linq expression more easily comparable even if the algorithm that
    generates the underlying variable numbers changes. If the selection algorithm will violate
    the qastle syntax, it is returned unaltered and with no errors.

    Arguments

        q           Strign containing the qastle code`

    Returns

        clean_q     Sanitized query - lambda arguments are given a uniform source code
                    labeling so that two queries with the same structure are marked as the same.
    '''
    from collections import namedtuple
    ParseTracker = namedtuple('ParseTracker', ['text', 'info'])

    arg_index = 0

    def new_arg():
        nonlocal arg_index
        s = f'a{arg_index}'
        arg_index += 1
        return s

    def _replace_strings(s: str, rep: Dict[str, str]) -> str:
        'Replace all strings in dict that are found in s'
        for k in rep.keys():
            s = re.sub(f'\\b{k}\\b', rep[k], s)
        return s

    class translator(Transformer):
        def record(self, children):
            if (len(children) == 0
                    or isinstance(children[0], Token)
                    and children[0].type == 'WHITESPACE'):
                return ""
            else:
                return children[0].text

        def expression(self, children):
            for child in children:
                if isinstance(child, ParseTracker):
                    return child
            raise SyntaxError('Expression does not contain a node')

        def atom(self, children):
            child = children[0]
            return ParseTracker(child.value, child.value)

        def composite(self, children):
            fields = []
            node_type: Optional[str] = None
            for child in children:
                if isinstance(child, Token):
                    if child.type == 'NODE_TYPE':  # type: ignore
                        node_type = child.value  # type: ignore
                elif isinstance(child, ParseTracker):
                    fields.append(child)

            assert node_type is not None

            if node_type == 'lambda':
                if len(fields) != 2:
                    raise Exception(f'The qastle "{q}" is not valid - found a lambda expression'
                                    f'with {len(fields)} arguments - not the required 2!')
                arg_list = [f.info for f in fields[0].info]
                arg_mapping = {old: new_arg() for old in arg_list}
                fields[0] = ParseTracker(
                    f'(list {" ".join(arg_mapping[k] for k in arg_list)})',
                    [ParseTracker(arg_mapping[f], arg_mapping[f]) for f in arg_list])
                fields[1] = ParseTracker(_replace_strings(fields[1].text, arg_mapping),
                                         fields[1].info)

            return ParseTracker(f'({node_type} {" ".join([f.text for f in fields])})', fields)

    try:
        tree = parse(q)
        new_tree = translator().transform(tree)
        assert isinstance(new_tree, str)
        return new_tree
    except Exception:
        return q
