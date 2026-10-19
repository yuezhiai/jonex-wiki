from enum import Enum


class TaskKind(str, Enum):
    parse = "parse"
    compile = "compile"
    query = "query"
    outline = "outline"


class TaskStatus(str, Enum):
    queued = "queued"
    parsing = "parsing"
    compiling = "compiling"
    paused = "paused"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"
