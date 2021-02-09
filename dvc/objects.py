import json
import logging

from .dir_info import DirInfo
from .exceptions import DvcException

logger = logging.getLogger(__name__)


class ObjectError(DvcException):
    pass


class ObjectFormatError(ObjectError):
    pass


def _get_hash(path_info, tree, name, **kwargs):
    if tree.PARAM_CHECKSUM == name:
        return tree.get_hash(path_info, **kwargs)

    if name == "md5":
        from dvc.hash_info import HashInfo
        from dvc.utils import file_md5

        return HashInfo("md5", file_md5(path_info, tree, **kwargs))

    raise AssertionError(f"unsupported hash '{name}'")


class HashFile:
    def __init__(self, path_info, tree, hash_info):
        self.path_info = path_info
        self.tree = tree
        self.hash_info = hash_info

    def __len__(self):
        return 1

    def __str__(self):
        return f"object {self.hash_info}"

    def __bool__(self):
        return bool(self.hash_info)

    def check(self, odb):
        actual = _get_hash(self.path_info, self.tree, odb.tree.PARAM_CHECKSUM)

        logger.trace(
            "cache '%s' expected '%s' actual '%s'",
            self.path_info,
            self.hash_info,
            actual,
        )

        assert actual.name == self.hash_info.name
        if actual.value.split(".")[0] != self.hash_info.value.split(".")[0]:
            raise ObjectFormatError(f"{self} is corrupted")

    @classmethod
    def load(cls, odb, hash_info):
        return odb.get(hash_info)

    def save(self, odb, **kwargs):
        odb.add(self, **kwargs)


class File(HashFile):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.src = None

    @classmethod
    def stage(cls, odb, path_info, tree, **kwargs):
        hash_info = _get_hash(
            path_info, tree, odb.tree.PARAM_CHECKSUM, **kwargs
        )
        raw = odb.get(hash_info)
        obj = cls(raw.path_info, raw.tree, hash_info)
        obj.src = HashFile(path_info, tree, hash_info)
        return obj

    def save(self, odb, **kwargs):
        self.src.save(odb, **kwargs)


class Tree(HashFile):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.src_hash_info = None
        self.src_path_info = None
        self.src_tree = None

    def __len__(self):
        return self.hash_info.dir_info.nfiles

    def __iter__(self):
        for (
            key,
            hash_info,
        ) in self.hash_info.dir_info.trie.iteritems():  # noqa: B301
            yield (key, hash_info)

    @classmethod
    def save_dir_info(cls, odb, dir_info, hash_info=None):
        if hash_info and hash_info.name == odb.tree.PARAM_CHECKSUM:
            try:
                odb.check(hash_info)
                assert hash_info.dir_info == dir_info
                return hash_info
            except (FileNotFoundError, ObjectFormatError):
                pass

        from dvc.path_info import PathInfo
        from dvc.tree.memory import MemoryTree
        from dvc.utils import tmp_fname

        tree = MemoryTree(None, {})
        path_info = PathInfo(tmp_fname(""))
        with tree.open(path_info, "w") as fobj:
            json.dump(dir_info.to_list(), fobj, sort_keys=True)

        tmp_info = odb.tree.path_info / tmp_fname("")
        with tree.open(path_info, "rb") as fobj:
            odb.tree.upload_fobj(fobj, tmp_info)

        hash_info = _get_hash(tmp_info, odb.tree, odb.tree.PARAM_CHECKSUM)
        hash_info.value += odb.tree.CHECKSUM_DIR_SUFFIX
        hash_info.dir_info = dir_info
        hash_info.nfiles = dir_info.nfiles

        obj = HashFile(tmp_info, odb.tree, hash_info)
        odb.add(obj)

        return hash_info

    @classmethod
    def stage(cls, odb, path_info, tree, **kwargs):
        hash_info = _get_hash(
            path_info, tree, odb.tree.PARAM_CHECKSUM, **kwargs
        )
        hi = cls.save_dir_info(odb, hash_info.dir_info, hash_info)
        hi.size = hash_info.size
        raw = odb.get(hi)
        obj = cls(raw.path_info, raw.tree, hi)
        obj.src_hash_info = hash_info
        obj.src_path_info = path_info
        obj.src_tree = tree
        return obj

    @classmethod
    def load(cls, odb, hash_info):

        obj = odb.get(hash_info)

        try:
            with obj.tree.open(obj.path_info, "r") as fobj:
                raw = json.load(fobj)
        except ValueError as exc:
            raise ObjectFormatError(f"{obj} is corrupted") from exc

        if not isinstance(raw, list):
            logger.error(
                "dir cache file format error '%s' [skipping the file]",
                obj.path_info,
            )
            raise ObjectFormatError(f"{obj} is corrupted")

        dir_info = DirInfo.from_list(raw)
        hash_info.dir_info = dir_info
        hash_info.nfiles = dir_info.nfiles

        return cls(obj.path_info, obj.tree, hash_info)

    def save(self, odb, **kwargs):
        from dvc.progress import Tqdm

        assert self.src_hash_info.dir_info
        hi = self.save_dir_info(
            odb, self.src_hash_info.dir_info, self.hash_info
        )
        for entry_info, entry_hash in Tqdm(
            hi.dir_info.items(self.src_path_info),
            desc="Saving " + self.src_path_info.name,
            unit="file",
        ):
            entry_obj = HashFile(entry_info, self.src_tree, entry_hash)
            entry_obj.save(odb, **kwargs)
        cache_info = odb.tree.hash_to_path_info(hi.value)
        odb.tree.state.save(cache_info, hi)
        self.src_tree.state.save(self.src_path_info, hi)

    def filter(self, odb, prefix):
        hash_info = self.hash_info.dir_info.trie.get(prefix)
        if hash_info:
            return load(odb, hash_info)

        depth = len(prefix)
        dir_info = DirInfo()
        try:
            for key, value in self.hash_info.dir_info.trie.items(prefix):
                dir_info.trie[key[depth:]] = value
        except KeyError:
            return None

        return load(odb, self.save_dir_info(odb, dir_info))


def stage(odb, path_info, tree, **kwargs):
    if tree.isdir(path_info):
        return Tree.stage(odb, path_info, tree, **kwargs)
    return File.stage(odb, path_info, tree, **kwargs)


def check(odb, obj):
    odb.check(obj.hash_info)

    if isinstance(obj, Tree):
        for _, hash_info in obj:
            odb.check(hash_info)


def load(odb, hash_info):
    if hash_info.isdir:
        return Tree.load(odb, hash_info)
    return File.load(odb, hash_info)


def _get_dir_size(odb, dir_info):
    try:
        return sum(
            odb.tree.getsize(odb.tree.hash_to_path_info(hi.value))
            for _, hi in dir_info.items()
        )
    except FileNotFoundError:
        return None


def merge(odb, ancestor_info, our_info, their_info):
    assert our_info
    assert their_info

    if ancestor_info:
        ancestor = load(odb, ancestor_info).hash_info.dir_info
    else:
        ancestor = DirInfo()

    our = load(odb, our_info).hash_info.dir_info
    their = load(odb, their_info).hash_info.dir_info

    merged = our.merge(ancestor, their)
    hash_info = Tree.save_dir_info(odb, merged)
    hash_info.size = _get_dir_size(odb, merged)
    return hash_info
