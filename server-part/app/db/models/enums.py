import enum


class BookStatus(str, enum.Enum):
    ANNOUNCED   = "announced"
    DOWNLOADING = "downloading"
    PARTIAL     = "partial"
    READY       = "ready"
    DELETING    = "deleting"
    DELETED     = "deleted"
    ERROR       = "error"


class ChapterStatus(str, enum.Enum):
    ABSENT      = "absent"
    DOWNLOADING = "downloading"
    PARTIAL     = "partial"
    READY       = "ready"
    DELETING    = "deleting"
    DELETED     = "deleted"
    ERROR       = "error"


class ContentType(str, enum.Enum):
    SINGLE     = "single"
    SERIES     = "series"
    COLLECTION = "collection"


class CoverOrigin(str, enum.Enum):
    TRACKER = "tracker"
    TORRENT = "torrent"
    SERIES  = "series"
    UPLOAD  = "upload"
    ONLINE  = "online"


class PersonRole(str, enum.Enum):
    AUTHOR      = "author"
    NARRATOR    = "narrator"
    TRANSLATOR  = "translator"
    EDITOR      = "editor"
    ILLUSTRATOR = "illustrator"


class DownloadFileStatus(str, enum.Enum):
    """Статус файла загрузки внутри торрента."""
    ABSENT      = "absent"
    DOWNLOADING = "downloading"
    PARTIAL     = "partial"
    READY       = "ready"
    ERROR       = "error"


class RubricKind(str, enum.Enum):
    GENRE  = "genre"
    TAG    = "tag"
    RUBRIC = "rubric"
