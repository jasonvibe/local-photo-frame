import io
from pathlib import Path
import struct
import pytest
from PIL import Image
import app as module

@pytest.fixture
def client(tmp_path, monkeypatch):
    for directory in ('images', 'recycle', 'thumbnails'):
        (tmp_path / directory).mkdir()
    monkeypatch.setattr(module, 'IMAGE_DIRECTORY', str(tmp_path / 'images'))
    monkeypatch.setattr(module, 'RECYCLE', tmp_path / 'recycle')
    monkeypatch.setattr(module, 'THUMBNAILS', tmp_path / 'thumbnails')
    monkeypatch.setattr(module, 'SETTINGS_FILE', str(tmp_path / 'settings.txt'))
    return module.app.test_client()

def jpeg():
    stream = io.BytesIO()
    image = Image.new('RGB', (1200, 800), '#336699')
    exif = Image.Exif(); exif[274] = 6
    image.save(stream, 'JPEG', exif=exif, quality=97)
    return stream.getvalue()

def box(box_type, payload):
    return struct.pack('>I', 8 + len(payload)) + box_type + payload

def tkhd(width, height, rot90=False):
    matrix = struct.pack('>9i', 0, 0x10000, 0, -0x10000, 0, 0, 0, 0, 0x40000000) if rot90 else \
             struct.pack('>9i', 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    payload = struct.pack('>I', 0)               # version 0 + flags
    payload += b'\x00' * 20                      # times, track id, reserved, duration
    payload += b'\x00' * 8                       # reserved
    payload += struct.pack('>hhhh', 0, 0, 0, 0)  # layer, alternate group, volume, reserved
    payload += matrix
    payload += struct.pack('>II', width * 65536, height * 65536)
    return box(b'tkhd', payload)

def mp4(width, height, rot90=False, faststart=True):
    ftyp = box(b'ftyp', b'isom\x00\x00\x02\x00isomiso2avc1mp41')
    moov = box(b'moov', box(b'trak', tkhd(width, height, rot90)))
    mdat = box(b'mdat', b'\x00' * 64)
    return ftyp + moov + mdat if faststart else ftyp + mdat + moov

def ebml_id(value):
    return value.to_bytes((value.bit_length() + 7) // 8, 'big')

def ebml_size(value):
    length = max(1, (value.bit_length() + 6) // 7)
    return ((1 << (7 * length)) | value).to_bytes(length, 'big')

def ebml_element(identifier, payload):
    return ebml_id(identifier) + ebml_size(len(payload)) + payload

def webm(width, height):
    header = ebml_element(0x1A45DFA3, ebml_element(0x4282, b'webm'))
    video = ebml_element(0xE0, ebml_element(0xB0, width.to_bytes(2, 'big')) + ebml_element(0xBA, height.to_bytes(2, 'big')))
    track = ebml_element(0xAE, ebml_element(0x83, b'\x01') + video)
    return header + ebml_element(0x18538067, ebml_element(0x1654AE6B, track))

def upload(client, data, name='test.jpg', original='1'):
    return client.post('/upload', data={'file': (io.BytesIO(data), name), 'original': original}, headers={'Accept':'application/json'})

def test_original_thumbnail_and_recycle(client):
    data = jpeg()
    result = upload(client, data)
    assert result.status_code == 200
    name = result.json['uploaded'][0]
    assert client.get('/image/' + name).data == data
    thumbnail = client.get('/thumbnail/' + name)
    assert thumbnail.status_code == 200
    image = Image.open(io.BytesIO(thumbnail.data))
    assert image.size == (320, 480)  # EXIF portrait orientation respected
    assert client.get('/image/' + name).data == data
    assert len(client.get('/api/photos').json) == 1
    cached = client.get('/thumbnail/' + name, headers={'If-None-Match': thumbnail.headers['ETag']})
    assert cached.status_code == 304
    deleted = client.post('/api/delete', json={'filenames':[name, name, '../settings.txt']})
    assert deleted.json == {'deleted':[name], 'failed':['../settings.txt']}
    assert client.get('/images').json == []
    recycled = list(module.RECYCLE.iterdir())
    assert len(recycled) == 1 and recycled[0].read_bytes() == data

def test_bad_and_partial_upload(client):
    assert upload(client, b'not an image').status_code == 400
    assert upload(client, jpeg(), 'x.heic').status_code == 400
    assert client.get('/images').json == []
    assert list(Path(module.IMAGE_DIRECTORY).iterdir()) == []
    result=client.post('/upload', data={'file':[(io.BytesIO(jpeg()),'good.jpg'),(io.BytesIO(b'bad'),'bad.png')],'original':'1'}, headers={'Accept':'application/json'})
    assert len(result.json['uploaded']) == 1 and len(result.json['errors']) == 1

def test_settings_validation_and_template(client):
    before=module.read_settings()
    assert client.post('/upload', data={'nextImage':'oops'}, headers={'Accept':'application/json'}).status_code==400
    assert module.read_settings()==before
    values=dict(nextImage='15',refreshImages='10',imageWidth='1920',imageHeight='1280',rotateImages='0')
    assert client.post('/upload',data=values,headers={'Accept':'application/json'}).status_code==200
    assert module.read_settings()['nextImage']==15
    page=client.get('/upload')
    assert page.status_code==200 and '上传照片' in page.text and 'lang="zh-CN"' in page.text
    assert 'Upload Images' not in page.text
    assert client.post('/api/delete', json={'filenames':[]}).status_code==400
    assert client.get('/image/settings.txt').status_code==404

def test_display_control_validates_and_reports_real_state(client, monkeypatch):
    class FakeDisplay:
        off = False
        calls = []
        def status(self):
            return dict(available=True, off=self.off, state='off' if self.off else 'on', message=None)
        def set_off(self, off):
            self.calls.append(off)
            self.off = off
            return self.status()
    display = FakeDisplay()
    monkeypatch.setattr(module, 'display_control', display)
    assert client.get('/api/display').json['off'] is False
    for invalid in ({'off':'true'}, {'off':1}, {}, [], None):
        assert client.post('/api/display', json=invalid).status_code == 400
    assert display.calls == []
    response = client.post('/api/display', json={'off':True})
    assert response.json['confirmed'] is True and response.json['off'] is True
    assert client.post('/api/display', json={'off':False}).json['off'] is False
    assert display.calls == [True,False]
    # Hardware can wake independently; the next GET must not show a cached request state.
    display.off = True
    assert client.get('/api/display').json['off'] is True
    display.off = False
    assert client.get('/api/display').json['off'] is False

def test_display_control_failures_are_not_reported_as_success(client, monkeypatch):
    class FailedDisplay:
        def set_off(self, off):
            raise OSError('test failure')
    monkeypatch.setattr(module, 'display_control', FailedDisplay())
    assert client.post('/api/display', json={'off':True}).status_code == 503
    class UnconfirmedDisplay:
        def set_off(self, off):
            return dict(available=True, off=False, state='on', message=None)
    monkeypatch.setattr(module, 'display_control', UnconfirmedDisplay())
    result = client.post('/api/display', json={'off':True}).json
    assert result['confirmed'] is False and result['off'] is False

def test_playlist_metadata_tracks_upload_and_delete_without_decoding_originals(client, monkeypatch):
    first = upload(client, jpeg()).json['uploaded'][0]
    module.image_ratio.cache_clear()
    response = client.get('/api/playlist')
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.json == [{'name': first, 'r': 800 / 1200}]
    # Subsequent polls use cached dimensions; no full-resolution decode is needed.
    def unexpected_open(*args, **kwargs):
        raise AssertionError('unchanged original opened again')
    with monkeypatch.context() as patch:
        patch.setattr(module.Image, 'open', unexpected_open)
        assert client.get('/api/playlist').json == response.json
    second = upload(client, jpeg(), 'new.jpg').json['uploaded'][0]
    assert {x['name'] for x in client.get('/api/playlist').json} == {first, second}
    client.post('/api/delete', json={'filenames': [first]})
    assert client.get('/api/playlist').json == [{'name': second, 'r': 800 / 1200}]
    client.post('/api/delete', json={'filenames': [second]})
    assert client.get('/api/playlist').json == []

def test_video_upload_playlist_and_removal(client):
    data = mp4(1920, 1080)
    result = upload(client, data, 'clip.mp4')
    assert result.status_code == 200
    name = result.json['uploaded'][0]
    assert client.get('/image/' + name).data == data
    assert client.get('/thumbnail/' + name).status_code == 404
    photos = client.get('/api/photos').json
    assert photos[0]['name'] == name and photos[0]['v'] == 1
    module.video_dimensions.cache_clear()
    assert client.get('/api/playlist').json == [{'name': name, 'r': 1920 / 1080, 'v': 1}]
    assert client.post('/api/delete', json={'filenames': [name]}).json == {'deleted': [name], 'failed': []}
    module.video_dimensions.cache_clear()
    assert client.get('/api/playlist').json == []

def test_video_rotation_and_tail_moov(client):
    rotated = upload(client, mp4(1920, 1080, rot90=True), 'rotate.mp4').json['uploaded'][0]
    tail = upload(client, mp4(640, 480, faststart=False), 'tail.mp4').json['uploaded'][0]
    module.video_dimensions.cache_clear()
    items = {x['name']: x for x in client.get('/api/playlist').json}
    assert items[rotated] == {'name': rotated, 'r': 1080 / 1920, 'v': 1}
    assert items[tail] == {'name': tail, 'r': 640 / 480, 'v': 1}

def test_webm_vertical_dimensions(client):
    name = upload(client, webm(1080, 1920), 'vertical.webm').json['uploaded'][0]
    module.video_dimensions.cache_clear()
    assert client.get('/api/playlist').json == [{'name': name, 'r': 1080 / 1920, 'v': 1}]

def test_video_validation_rejects_broken_and_oversized(client, monkeypatch):
    assert upload(client, b'not-a-real-video-file', 'fake.mp4').status_code == 400
    assert upload(client, webm(64, 64), 'movie.avi').status_code == 400
    monkeypatch.setattr(module, 'VIDEO_MAX_BYTES', 10)
    assert upload(client, mp4(64, 64), 'big.mp4').status_code == 400
    assert client.get('/images').json == []
    assert list(Path(module.IMAGE_DIRECTORY).iterdir()) == []
