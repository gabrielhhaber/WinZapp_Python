"""Video messages without ffprobe.

neonize's `build_video_message` runs `ffprobe` for the duration and again,
inside `extract_thumbnail`, before scaling the thumbnail; WinZapp does not ship
ffprobe, so every video failed. `build` makes the same message: the duration
from the MP4 itself (media_probe.py), the thumbnail from one run of the bundled
ffmpeg, whose stderr also gives the duration of any other container. A video
whose thumbnail ffmpeg cannot make still goes out, without the preview.

Tested in test_video_message.py.
"""
import media_probe


def build(client, file, caption=None, mimetype_of=None, probe=None):
    """The video Message neonize's `build_video_message` would build, minus
    ffprobe. `file` is a path or the bytes; the upload goes through
    `client.upload`, so the progress client of transfers.py still sees it."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ContextInfo, Message, VideoMessage
    data = media_probe.read(file)
    seconds = media_probe.mp4_seconds(data)
    thumbnail, ffmpeg_seconds = (probe or media_probe.thumbnail_and_seconds)(file)
    upload = client.upload(data, media_probe.media_type_of(data, lambda d: "video/mp4"))
    return Message(videoMessage=VideoMessage(
        URL=upload.url, caption=caption, directPath=upload.DirectPath,
        mediaKey=upload.MediaKey, fileEncSHA256=upload.FileEncSHA256,
        fileSHA256=upload.FileSHA256, fileLength=upload.FileLength,
        seconds=media_probe.whole_seconds(seconds if seconds is not None else ffmpeg_seconds),
        mimetype=(mimetype_of or media_probe.mimetype)(data) or "video/mp4",
        # the thumbnail fields as neonize fills them
        JPEGThumbnail=thumbnail, thumbnailDirectPath=upload.DirectPath,
        thumbnailEncSHA256=upload.FileEncSHA256, thumbnailSHA256=upload.FileSHA256,
        contextInfo=ContextInfo(mentionedJID=client._parse_mention(caption),
                                groupMentions=client._parse_group_mention(caption))))
