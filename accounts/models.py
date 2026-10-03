import uuid
from pathlib import Path

from django.contrib.auth.models import AbstractUser
from django.db import models
from utils.validators import validate_document_upload, validate_image_upload


def _profile_upload(instance, filename):
    return f'profiles/{uuid.uuid4().hex}{Path(filename).suffix.lower()}'


def _rider_doc_upload(instance, filename):
    return f'rider_docs/{uuid.uuid4().hex}{Path(filename).suffix.lower()}'


class User(AbstractUser):
    ROLE_CHOICES = [
        ('customer', 'Customer'),
        ('shop_owner', 'Shop Owner'),
        ('rider', 'Rider'),
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

    def __str__(self):
        return f'{self.get_full_name() or self.email} ({self.role})'

    def save(self, *args, **kwargs):
        from utils.images import shrink_uploaded_image
        shrink_uploaded_image(self.profile_picture, max_px=600)   # shown as a small avatar
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
