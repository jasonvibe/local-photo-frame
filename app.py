import hashlib
from functools import lru_cache
import os
import struct
import threading
import time
from pathlib import Path
from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.utils import secure_filename
from display_control import DisplayControl

app = Flask(__name__)
display_control = DisplayControl()
BASE = Path(__file__).resolve().parent
IMAGE_DIRECTORY = str(BASE / 'images')
SETTINGS_FILE = str(BASE / 'settings.txt')
RECYCLE = BASE / 'recycle'
THUMBNAILS = BASE / 'thumbnails'
IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
VIDEO_EXTENSIONS = {'mp4', 'webm', 'mov'}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
IMAGE_MAX_BYTES = 50 * 1024 * 1024
VIDEO_MAX_BYTES = 500 * 1024 * 1024
EBML_SIGNATURE = b'\x1a\x45\xdf\xa3'
thumb_lock = threading.Lock()
for directory in (Path(IMAGE_DIRECTORY), RECYCLE, THUMBNAILS):
    directory.mkdir(exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def is_video(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in VIDEO_EXTENSIONS

def _read_ebml_vint(handle, position, end, keep_marker):
    """Read an EBML variable-size integer; returns (value, next_position)."""
    if position >= end:
        return None, None
    handle.seek(position)
    chunk = handle.read(8)
    if len(chunk) < 1 or chunk[0] == 0:
        return None, None
    length = 9 - chunk[0].bit_length()  # leading zero bits encode the total length
    if position + length > end or len(chunk) < length:
        return None, None
    raw = int.from_bytes(chunk[:length], 'big')
    if keep_marker:
        return raw, position + length
    if length == 8 and raw == (1 << 56) - 1:
        return None, position + length  # unknown size marker
    return raw & ((1 << (7 * length)) - 1), position + length

def _scan_mp4_boxes(handle, start, end, depth):
    position, best = start, None
    while position + 8 <= end:
        handle.seek(position)
        header = handle.read(16)
        if len(header) < 8:
            return best
        box_size, box_type = struct.unpack('>I4s', header[:8])
        content = position + 8
        if box_size == 1:
            if len(header) < 16:
                return best
            box_size = struct.unpack('>Q', header[8:16])[0]
            content = position + 16
        elif box_size == 0:
            box_size = end - position
        if box_size < content - position or position + box_size > end:
            return best
        if box_type in (b'moov', b'trak') and depth < 2:
            found = _scan_mp4_boxes(handle, content, position + box_size, depth + 1)
        elif box_type == b'tkhd':
            found = _parse_tkhd(handle, content, position + box_size)
        else:
            found = None
        if found and (best is None or found[0] * found[1] > best[0] * best[1]):
            best = found
        position += box_size
    return best

def _parse_tkhd(handle, start, end):
    """tkhd carries display size as 16.16 fixed point plus a rotation matrix."""
    handle.seek(start)
    data = handle.read(96)
    if len(data) < 84:
        return None
    if data[0] == 1:
        if len(data) < 96:
            return None
        raw_width, raw_height = struct.unpack_from('>II', data, 88)
        matrix = struct.unpack_from('>9i', data, 52)
    else:
        raw_width, raw_height = struct.unpack_from('>II', data, 76)
        matrix = struct.unpack_from('>9i', data, 40)
    width, height = raw_width / 65536, raw_height / 65536
    if width <= 0 or height <= 0:
        return None
    if abs(matrix[1]) > abs(matrix[0]):  # 90/270 degree rotation swaps axes
        width, height = height, width
    return (round(width), round(height))

def _mp4_video_dimensions(path):
    try:
        file_size = path.stat().st_size
        with open(path, 'rb') as handle:
            return _scan_mp4_boxes(handle, 0, file_size, 0)
    except OSError:
        return None

def _scan_webm_tracks(handle, start, end):
    position = start
    while position < end:
        element_id, position = _read_ebml_vint(handle, position, end, True)
        if element_id is None:
            return None
        size, position = _read_ebml_vint(handle, position, end, False)
        if element_id != 0x1654AE6B:  # Tracks
            if size is None:
                return None
            position += size
            continue
        child_end = end if size is None else min(position + size, end)
        entry = _read_webm_track_entries(handle, position, child_end)
        if entry:
            return entry
        if size is None:
            return None
        position += size
    return None

def _read_webm_track_entries(handle, start, end):
    position = start
    while position < end:
        element_id, position = _read_ebml_vint(handle, position, end, True)
        if element_id is None:
            return None
        size, position = _read_ebml_vint(handle, position, end, False)
        if size is None:
            return None
        payload_end = min(position + size, end)
        if element_id == 0xAE:  # TrackEntry
            entry = _read_webm_track_entry(handle, position, payload_end)
            if entry:
                return entry
        position = payload_end
    return None

def _read_webm_track_entry(handle, start, end):
    position, width, height, is_video_track = start, 0, 0, False
    while position < end:
        element_id, position = _read_ebml_vint(handle, position, end, True)
        if element_id is None:
            break
        size, position = _read_ebml_vint(handle, position, end, False)
        if size is None:
            break
        payload_end = min(position + size, end)
        if element_id == 0x83 and size <= 8:  # TrackType: 1 = video
            handle.seek(position)
            is_video_track = int.from_bytes(handle.read(size), 'big') == 1
        elif element_id == 0xE0:  # Video
            dimensions = _read_webm_video_element(handle, position, payload_end)
            if dimensions:
                width, height = dimensions
        position = payload_end
    return (width, height) if is_video_track and width > 0 and height > 0 else None

def _read_webm_video_element(handle, start, end):
    position, width, height = start, 0, 0
    while position < end:
        element_id, position = _read_ebml_vint(handle, position, end, True)
        if element_id is None:
            break
        size, position = _read_ebml_vint(handle, position, end, False)
        if size is None:
            break
        payload_end = min(position + size, end)
        if element_id == 0xB0 and size <= 8:  # PixelWidth
            handle.seek(position)
            width = int.from_bytes(handle.read(size), 'big')
        elif element_id == 0xBA and size <= 8:  # PixelHeight
            handle.seek(position)
            height = int.from_bytes(handle.read(size), 'big')
        position = payload_end
    return (width, height) if width > 0 and height > 0 else None

def _webm_video_dimensions(path):
    try:
        file_size = path.stat().st_size
        with open(path, 'rb') as handle:
            element_id, position = _read_ebml_vint(handle, 0, file_size, True)
            if element_id != 0x1A45DFA3:  # EBML header
                return None
            size, position = _read_ebml_vint(handle, position, file_size, False)
            if size is not None:
                position += size
            segment_id, position = _read_ebml_vint(handle, position, file_size, True)
            if segment_id != 0x18538067:  # Segment
                return None
            size, position = _read_ebml_vint(handle, position, file_size, False)
            segment_end = file_size if size is None else min(position + size, file_size)
            # Track headers live near the start; scanning a bounded window avoids
            # walking a potentially huge file.
            return _scan_webm_tracks(handle, position, min(segment_end, position + 32 * 1024 * 1024))
    except OSError:
        return None

@lru_cache(maxsize=2048)
def video_dimensions(path, modified_ns, size):
    path = Path(path)
    dimensions = _webm_video_dimensions(path) if path.suffix.lower() == '.webm' else _mp4_video_dimensions(path)
    return dimensions if dimensions else (16, 9)

def read_settings():
    settings = dict(nextImage=15, refreshImages=10, imageWidth=1920, imageHeight=1280, rotateImages=0)
    if os.path.exists(SETTINGS_FILE):
        for line in Path(SETTINGS_FILE).read_text(encoding='utf-8-sig').splitlines():
            key, _, value = line.partition('=')
            if key in settings:
                try:
                    settings[key] = int(value)
                except ValueError:
                    pass
    return settings

def write_settings(next_image, refresh_images, image_width, image_height, rotate_images):
    values = (next_image, refresh_images, image_width, image_height, rotate_images)
    keys = ('nextImage', 'refreshImages', 'imageWidth', 'imageHeight', 'rotateImages')
    temporary = SETTINGS_FILE + '.pending'
    Path(temporary).write_text(''.join(f'{k}={v}\n' for k, v in zip(keys, values)), encoding='utf-8')
    os.replace(temporary, SETTINGS_FILE)

def image_names():
    return [p.name for p in Path(IMAGE_DIRECTORY).iterdir() if p.is_file() and allowed_file(p.name)]

def valid_path(filename):
    if not isinstance(filename, str) or '/' in filename or '\\' in filename or not allowed_file(filename):
        return None
    path = Path(IMAGE_DIRECTORY) / filename
    return path if path.is_file() else None

def wants_json():
    return request.accept_mimetypes.best == 'application/json'

@app.route('/api/display', methods=['GET', 'POST'])
def display_power():
    if request.method == 'GET':
        return jsonify(display_control.status())
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get('off')) is not bool:
        return jsonify(message='请指定息屏开关状态'), 400
    try:
        status = display_control.set_off(data['off'])
    except OSError:
        return jsonify(message='屏幕控制失败，请稍后重试或触碰 相框设备 唤醒屏幕'), 503
    status['confirmed'] = status['off'] is data['off']
    status['message'] = ('已进入兼容息屏（黑屏、最低背光，保持联网）' if data['off'] else '已恢复照片和自动亮度') if status['confirmed'] else '指令已发送，等待系统确认屏幕状态'
    return jsonify(status)

@app.route('/image/<filename>')
def get_image(filename):
    if valid_path(filename) is None:
        abort(404)
    return send_from_directory(IMAGE_DIRECTORY, filename)

@app.route('/thumbnail/<filename>')
def thumbnail(filename):
    source = valid_path(filename)
    if source is None or is_video(filename):
        abort(404)
    stat = source.stat()
    digest = hashlib.sha256(f'{filename}:{stat.st_mtime_ns}:{stat.st_size}'.encode()).hexdigest()
    target = THUMBNAILS / (digest + '.jpg')
    if not target.exists():
        with thumb_lock:
            if not target.exists():
                try:
                    with Image.open(source) as original:
                        original.draft('RGB', (480, 480))
                        image = ImageOps.exif_transpose(original)
                        image.thumbnail((480, 480), Image.Resampling.LANCZOS)
                        rgba = image.convert('RGBA')
                        output = Image.new('RGB', rgba.size, 'white')
                        output.paste(rgba, mask=rgba.getchannel('A'))
                        output.save(target, 'JPEG', quality=83)
                except (OSError, ValueError, Image.DecompressionBombError):
                    abort(422)
    return send_from_directory(str(THUMBNAILS), target.name, max_age=86400, conditional=True)

@app.route('/upload', methods=['GET', 'POST'])
def upload_image():
    settings = read_settings()
    if request.method == 'GET':
        return render_template('upload.html', settings=settings)
    if 'file' in request.files:
        uploaded, errors = [], []
        for file in request.files.getlist('file'):
            name = file.filename or ''
            if not allowed_file(name):
                errors.append({'name': name, 'message': '格式暂不支持，请选择 JPG、PNG、GIF 或 MP4、WebM、MOV 文件'})
                continue
            safe = secure_filename(name)
            extension = name.rsplit('.', 1)[1].lower()
            stem = Path(safe).stem[:100] or 'photo'
            unique = f'{stem}_{time.time_ns()}.{extension}'
            temporary = Path(IMAGE_DIRECTORY) / (unique + '.pending')
            try:
                file.save(temporary)
                if is_video(name):
                    if temporary.stat().st_size > VIDEO_MAX_BYTES:
                        raise ValueError('视频请小于 500 MB')
                    with open(temporary, 'rb') as check:
                        magic = check.read(12)
                    if magic[4:8] != b'ftyp' and not magic.startswith(EBML_SIGNATURE):
                        raise ValueError('视频容器无法识别，请上传标准 MP4 / WebM / MOV 文件')
                else:
                    if temporary.stat().st_size > IMAGE_MAX_BYTES:
                        raise ValueError('单张照片请小于 50 MB')
                    with Image.open(temporary) as check:
                        check.verify()
                    if request.form.get('original') != '1':
                        with Image.open(temporary) as source:
                            image = ImageOps.exif_transpose(source)
                            image.thumbnail((settings['imageWidth'], settings['imageHeight']), Image.Resampling.LANCZOS)
                            if extension in ('jpg', 'jpeg'):
                                image = image.convert('RGB')
                            image.save(temporary, format={'jpg': 'JPEG', 'jpeg': 'JPEG', 'png': 'PNG', 'gif': 'GIF'}[extension])
                os.replace(temporary, Path(IMAGE_DIRECTORY) / unique)
                uploaded.append(unique)
            except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as error:
                errors.append({'name': name, 'message': str(error) if isinstance(error, ValueError) else '照片无法读取，请换一张或重新导出后上传'})
            finally:
                temporary.unlink(missing_ok=True)
        if wants_json():
            return jsonify(uploaded=uploaded, errors=errors), (200 if uploaded else 400)
        return redirect(url_for('upload_image', uploaded=len(uploaded), failed=len(errors)))
    try:
        values = [int(request.form[k]) for k in ('nextImage', 'refreshImages', 'imageWidth', 'imageHeight', 'rotateImages')]
        limits = ((3, 3600), (5, 3600), (320, 8192), (320, 8192))
        if any(not low <= value <= high for value, (low, high) in zip(values[:4], limits)) or values[4] not in (0, 90, 180, 270):
            raise ValueError()
        write_settings(*values)
    except (KeyError, ValueError, TypeError):
        return jsonify(message='设置未保存，请检查数字范围'), 400
    if wants_json():
        return jsonify(message='播放设置已保存')
    return redirect(url_for('upload_image', saved=1))

def recycle_image(filename):
    path = valid_path(filename)
    if path is None:
        return False
    os.replace(path, RECYCLE / f'{time.time_ns()}_{filename}')
    return True

@app.route('/delete/<filename>', methods=['POST'])
def delete_image(filename):
    recycle_image(filename)
    return redirect(url_for('upload_image'))

@app.route('/api/delete', methods=['POST'])
def delete_batch():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(message='请先选择要删除的照片'), 400
    names = data.get('filenames')
    if not isinstance(names, list) or not names or len(names) > 1000 or any(not isinstance(n, str) for n in names):
        return jsonify(message='请先选择要删除的照片'), 400
    deleted, failed = [], []
    for name in dict.fromkeys(names):
        try:
            (deleted if recycle_image(name) else failed).append(name)
        except OSError:
            failed.append(name)
    return jsonify(deleted=deleted, failed=failed)

@app.route('/api/photos')
def photo_list():
    photos = []
    for name in image_names():
        try:
            info = (Path(IMAGE_DIRECTORY) / name).stat()
            entry = dict(name=name, modified=info.st_mtime_ns // 1000000, size=info.st_size)
            if is_video(name):
                entry['v'] = 1
            photos.append(entry)
        except FileNotFoundError:
            pass
    return jsonify(sorted(photos, key=lambda p: (p['modified'], p['name']), reverse=True))

@lru_cache(maxsize=2048)
def image_ratio(path, modified_ns, size):
    # Cache header metadata only; never decode or resize the original image.
    try:
        with Image.open(path) as source:
            width, height = source.size
            if source.getexif().get(274, 1) in (5, 6, 7, 8):
                width, height = height, width
            return width / height
    except (OSError, ValueError, Image.DecompressionBombError):
        return None

@app.route('/api/playlist')
def playback_playlist():
    items = []
    for name in image_names():
        path = Path(IMAGE_DIRECTORY) / name
        try:
            info = path.stat()
            if is_video(name):
                width, height = video_dimensions(str(path), info.st_mtime_ns, info.st_size)
                items.append(dict(name=name, r=width / height, v=1))
                continue
            ratio = image_ratio(str(path), info.st_mtime_ns, info.st_size)
        except FileNotFoundError:
            continue
        if ratio is not None:
            items.append(dict(name=name, r=ratio))
    response = jsonify(items)
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/images')
def get_images():
    return jsonify(image_names())

@app.route('/settings')
def get_settings():
    return jsonify(read_settings())

@app.route('/')
def index():
    return render_template('gallery.html', images=image_names(), settings=read_settings())

if __name__ == '__main__':
    from waitress import serve
    serve(app, host='127.0.0.1', port=4444, threads=2)
