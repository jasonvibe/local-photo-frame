import hashlib
from functools import lru_cache
import os
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
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
thumb_lock = threading.Lock()
for directory in (Path(IMAGE_DIRECTORY), RECYCLE, THUMBNAILS):
    directory.mkdir(exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

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
    if source is None:
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
                errors.append({'name': name, 'message': '格式暂不支持，请选择 JPG、PNG 或 GIF 照片'})
                continue
            safe = secure_filename(name)
            extension = name.rsplit('.', 1)[1].lower()
            stem = Path(safe).stem[:100] or 'photo'
            unique = f'{stem}_{time.time_ns()}.{extension}'
            temporary = Path(IMAGE_DIRECTORY) / (unique + '.pending')
            try:
                file.save(temporary)
                if temporary.stat().st_size > 50 * 1024 * 1024:
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
            photos.append(dict(name=name, modified=info.st_mtime_ns // 1000000, size=info.st_size))
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
