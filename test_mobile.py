import io
from pathlib import Path
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
