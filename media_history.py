"""One-time index of legacy UUID media without pretending it has a known request."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import stat
import tempfile

_NAME=re.compile(r"^[0-9a-fA-F]{32}\.(?:png|jpe?g|webp|gif|avif|mp4|webm|mov|mp3|wav|m4a|ogg|flac|aac|opus)$")


def index_legacy_media(store, media_dir, marker_path, media_url_fn):
    marker=Path(marker_path)
    if marker.exists():return {"indexed":0,"already_indexed":True}
    root=Path(media_dir)
    if not root.is_dir() or root.is_symlink():return {"indexed":0,"skipped":True}
    known=set()
    for task in list(store.tasks.values()):
        result=task.get("result") or {}
        if isinstance(result,dict):
            if isinstance(result.get("filename"),str):known.add(result["filename"])
        for url in (task.get("url"),result.get("url") if isinstance(result,dict) else None):
            if isinstance(url,str) and "/v1/media/" in url:
                known.add(url.rsplit("/v1/media/",1)[-1].split("?",1)[0])
    indexed=0
    for entry in sorted(os.scandir(root),key=lambda e:e.name):
        if entry.name in known or not _NAME.fullmatch(entry.name):continue
        try:info=entry.stat(follow_symlinks=False)
        except OSError:continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:continue
        suffix=Path(entry.name).suffix.lower()
        kind="video" if suffix in (".mp4",".webm",".mov") else "audio" if suffix in (".mp3",".wav",".m4a",".ogg",".flac",".aac",".opus") else "image"
        task=store.create_task(kind,"历史媒体（无原请求记录）")
        store.update_task(task["id"],status="completed",progress=100,created_at=int(info.st_mtime),
                          source="media_history",historical_media=True,generation={},
                          url=media_url_fn(entry.name),
                          result={"filename":entry.name,"url":media_url_fn(entry.name),"bytes":info.st_size,"kind":kind})
        known.add(entry.name);indexed+=1
    marker.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix=".media-index-",dir=marker.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as out:
            json.dump({"version":1,"indexed":indexed},out);out.flush();os.fsync(out.fileno())
        os.replace(temporary,marker)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    return {"indexed":indexed,"already_indexed":False}
