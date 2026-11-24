import uuid
from pathlib import Path

from django.contrib.auth.models import AbstractUser
from django.db import models
from utils.validators import validate_document_upload, validate_image_upload


def _profile_upload(instance, filename):
    return f'profiles/{uuid.uuid4().hex}{Path(filename).suffix.lower()}'


def _rider_doc_upload(instance, filename):
    return f'rider_docs/{uuid.uuid4().hex}{Path(filename).suffix.lower()}'


def _face_selfie_upload(instance, filename):
    return f'face_selfies/{uuid.uuid4().hex}{Path(filename).suffix.lower()}'


def _id_document_upload(instance, filename):
    return f'id_documents/{uuid.uuid4().hex}{Path(filename).suffix.lower()}'


class User(AbstractUser):
    ROLE_CHOICES = [
        ('customer', 'Customer'),
        ('shop_owner', 'Shop Owner'),
        ('rider', 'Rider'),
        ('attendant', 'Shop Attendant'),     # created by a shop owner; works one shop's counter
        ('admin', 'Administrator'),
    ]
    SSO_CHOICES = [
        ('none', 'None'),
        ('google', 'Google'),
        ('facebook', 'Facebook'),
    ]

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, blank=True, default='')
    contact_number = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    profile_picture = models.ImageField(upload_to=_profile_upload, blank=True, null=True, validators=[validate_image_upload])
    sso_provider = models.CharField(max_length=20, choices=SSO_CHOICES, default='none')
    email_notifications_enabled = models.BooleanField(default=True)
    email_verified = models.BooleanField(default=False)
    # Customer face check: live selfie that passed the in-browser liveness challenge.
    face_selfie = models.ImageField(upload_to=_face_selfie_upload, blank=True, null=True, validators=[validate_image_upload])
    face_verified_at = models.DateTimeField(null=True, blank=True)

    # Customer valid-ID check: the selfie above is matched server-side against this ID photo.
    ID_TYPE_CHOICES = [
        ('philsys', 'PhilSys National ID'),
        ('drivers_license', "Driver's License"),
        ('passport', 'Passport'),
        ('umid', 'UMID'),
        ('postal', 'Postal ID'),
        ('voters', "Voter's ID"),
        ('prc', 'PRC ID'),
        ('student', 'School / Student ID'),
        ('other', 'Other government ID'),
    ]
    ID_STATUS_CHOICES = [
        ('none', 'Not submitted'),
        ('pending_review', 'Pending review'),
        ('verified', 'Verified'),
        ('rejected', 'Rejected'),
    ]
    id_type = models.CharField(max_length=20, choices=ID_TYPE_CHOICES, blank=True)
    id_document = models.ImageField(upload_to=_id_document_upload, blank=True, null=True, validators=[validate_image_upload])
    id_status = models.CharField(max_length=20, choices=ID_STATUS_CHOICES, default='none')
    id_match_score = models.FloatField(null=True, blank=True)   # SFace cosine similarity; None = not computed
    id_submitted_at = models.DateTimeField(null=True, blank=True)
    id_reviewed_at = models.DateTimeField(null=True, blank=True)
    id_rejection_reason = models.CharField(max_length=500, blank=True)

    @property
    def needs_face_verification(self):
        """Customers must pass the live face check AND have a verified ID before booking."""
        if self.role != 'customer' or self.is_superuser:
            return False
        return not self.face_verified_at or self.id_status != 'verified'

    @property
    def loyalty_points(self):
        if not hasattr(self, '_loyalty_points'):
            from django.db.models import Sum
            self._loyalty_points = max(self.loyalty_entries.aggregate(s=Sum('points'))['s'] or 0, 0)
        return self._loyalty_points

    @property
    def cod_eligible(self):
        """Trusted customers (enough delivered + paid orders) may use cash on delivery."""
        if not hasattr(self, '_cod_eligible'):
            from django.conf import settings
            needed = settings.COD_TRUSTED_MIN_ORDERS
            self._cod_eligible = needed <= 0 or (
                self.orders.filter(status='delivered', payment_status='paid').count() >= needed
            )
        return self._cod_eligible

    def __str__(self):
        return f'{self.get_full_name() or self.email} ({self.role})'

    def save(self, *args, **kwargs):
        from utils.images import shrink_uploaded_image
        shrink_uploaded_image(self.profile_picture, max_px=600)   # shown as a small avatar
        shrink_uploaded_image(self.id_document, max_px=1600)      # keep ID text legible for admin review
        super().save(*args, **kwargs)


class RiderProfile(models.Model):
    VEHICLE_CHOICES = [
        ('motorcycle', 'Motorcycle'),
        ('car', 'Car'),
        ('bicycle', 'Bicycle'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='rider_profile')
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=30, blank=True)
    emergency_contact_name = models.CharField(max_length=120, blank=True)
    emergency_contact_relationship = models.CharField(max_length=60, blank=True)
    emergency_contact_number = models.CharField(max_length=30, blank=True)
    vehicle_type = models.CharField(max_length=30, choices=VEHICLE_CHOICES, blank=True)
    vehicle_make_model = models.CharField(max_length=120, blank=True)
    vehicle_year = models.PositiveIntegerField(null=True, blank=True)
    vehicle_color = models.CharField(max_length=60, blank=True)
    plate_number = models.CharField(max_length=40, blank=True)
    available_days = models.CharField(max_length=120, blank=True)
    shift_start = models.TimeField(null=True, blank=True)
    shift_end = models.TimeField(null=True, blank=True)
    coverage_areas = models.TextField(blank=True)
    max_radius_km = models.PositiveIntegerField(default=5)
    accepts_cod = models.BooleanField(default=True)
    allows_multiple_dropoffs = models.BooleanField(default=True)
    nearby_job_alerts = models.BooleanField(default=True)
    government_id_front = models.FileField(upload_to=_rider_doc_upload, blank=True, null=True, validators=[validate_document_upload])
    government_id_back = models.FileField(upload_to=_rider_doc_upload, blank=True, null=True, validators=[validate_document_upload])
    drivers_license_front = models.FileField(upload_to=_rider_doc_upload, blank=True, null=True, validators=[validate_document_upload])
    drivers_license_back = models.FileField(upload_to=_rider_doc_upload, blank=True, null=True, validators=[validate_document_upload])
    vehicle_cr = models.FileField(upload_to=_rider_doc_upload, blank=True, null=True, validators=[validate_document_upload])
    vehicle_or = models.FileField(upload_to=_rider_doc_upload, blank=True, null=True, validators=[validate_document_upload])
    selfie_with_id = models.FileField(upload_to=_rider_doc_upload, blank=True, null=True, validators=[validate_image_upload])
    is_submitted = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(null=True, blank=True)
    APPROVAL_CHOICES = [
        ('pending', 'Pending Review'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    approval_status = models.CharField(max_length=20, choices=APPROVAL_CHOICES, default='pending')
    rejection_reason = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    DOCUMENT_FIELDS = [
        'government_id_front', 'government_id_back',
        'drivers_license_front', 'drivers_license_back',
        'vehicle_cr', 'vehicle_or', 'selfie_with_id',
    ]

    def __str__(self):
        return f'Rider profile - {self.user.get_full_name() or self.user.email}'

    @property
    def is_approved(self):
        return self.approval_status == 'approved'


def approved_riders():
    """Riders an admin has verified: the only riders who may claim or be assigned jobs."""
    return User.objects.filter(role='rider', is_active=True, rider_profile__approval_status='approved')


def is_approved_rider(user):
    profile = getattr(user, 'rider_profile', None)
    return bool(user.is_active and user.role == 'rider' and profile and profile.is_approved)
