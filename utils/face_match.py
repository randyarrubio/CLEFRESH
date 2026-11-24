"""Server-side face matching: does the live selfie show the same person as the ID photo?

Uses OpenCV's YuNet face detector and SFace recognizer (models from the OpenCV model zoo,
stored in utils/face_models/). Both images are decoded here from the uploaded bytes, so the
browser never gets to say whether the faces match.

`compare()` never raises for bad input: it returns a FaceMatch whose `status` says what happened.
If OpenCV or the model files are missing, status is 'unavailable' and the caller should fall
back to manual admin review.
"""
import hashlib
import io
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parent / 'face_models'
DETECTOR_MODEL = MODEL_DIR / 'face_detection_yunet_2023mar.onnx'
RECOGNIZER_MODEL = MODEL_DIR / 'face_recognition_sface_2021dec.onnx'
MODEL_SHA256 = {
    DETECTOR_MODEL.name: '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4',
    RECOGNIZER_MODEL.name: '0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79',
}

# OpenCV's published cosine-similarity threshold for SFace: at or above = same person.
MATCH_THRESHOLD = 0.363
# Detection confidence. ID cards are small, printed and often glare-y, so this is a bit
# below YuNet's 0.9 default; the recognizer decides identity, not the detector.
DETECT_SCORE = 0.7
MAX_SIDE = 1280          # downscale big phone photos before detection
MIN_SIDE = 480           # upscale tiny crops so a small ID portrait is still detectable

_lock = threading.Lock()  # OpenCV DNN objects aren't safe to share across threads
_models = None


@dataclass
class FaceMatch:
    status: str            # matched | mismatch | no_face_selfie | no_face_id | unavailable
    score: float | None = None

    @property
    def matched(self):
        return self.status == 'matched'


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _load_models():
    global _models
    if _models is None:
        import cv2  # imported lazily so the site still runs without OpenCV installed
        for path in (DETECTOR_MODEL, RECOGNIZER_MODEL):
            if _sha256(path) != MODEL_SHA256[path.name]:
                raise RuntimeError(f'Face model checksum mismatch: {path.name}')
        detector = cv2.FaceDetectorYN.create(str(DETECTOR_MODEL), '', (320, 320), DETECT_SCORE, 0.3, 5000)
        recognizer = cv2.FaceRecognizerSF.create(str(RECOGNIZER_MODEL), '')
        _models = (cv2, detector, recognizer)
    return _models


def _decode(fileobj):
    """Uploaded file → upright BGR numpy array sized for detection."""
    import numpy as np
    from PIL import Image, ImageOps
    fileobj.seek(0)
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(fileobj.read()))).convert('RGB')
    fileobj.seek(0)
    longest = max(im.size)
    if longest > MAX_SIDE:
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    elif longest < MIN_SIDE:
        scale = MIN_SIDE / longest
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    return np.ascontiguousarray(np.asarray(im)[:, :, ::-1])


def _largest_face(detector, img):
    h, w = img.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(img)
    if faces is None or len(faces) == 0:
        return None
    return max(faces, key=lambda f: f[2] * f[3])   # biggest box = the ID portrait / the user


def compare(selfie_file, id_file):
    """Compare the face in a selfie with the face on an ID photo."""
    try:
        with _lock:
            cv2, detector, recognizer = _load_models()
            selfie_img, id_img = _decode(selfie_file), _decode(id_file)
            selfie_face = _largest_face(detector, selfie_img)
            if selfie_face is None:
                return FaceMatch('no_face_selfie')
            id_face = _largest_face(detector, id_img)
            if id_face is None:
                return FaceMatch('no_face_id')
            feat_selfie = recognizer.feature(recognizer.alignCrop(selfie_img, selfie_face))
            feat_id = recognizer.feature(recognizer.alignCrop(id_img, id_face))
            score = float(recognizer.match(feat_selfie, feat_id, cv2.FaceRecognizerSF_FR_COSINE))
    except Exception:
        logger.exception('Face matching unavailable; falling back to manual review')
        return FaceMatch('unavailable')
    return FaceMatch('matched' if score >= MATCH_THRESHOLD else 'mismatch', round(score, 3))
