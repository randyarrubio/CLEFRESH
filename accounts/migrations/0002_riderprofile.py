from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='RiderProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date_of_birth', models.DateField(blank=True, null=True)),
                ('gender', models.CharField(blank=True, max_length=30)),
                ('emergency_contact_name', models.CharField(blank=True, max_length=120)),
                ('emergency_contact_relationship', models.CharField(blank=True, max_length=60)),
                ('emergency_contact_number', models.CharField(blank=True, max_length=30)),
                ('vehicle_type', models.CharField(blank=True, choices=[('motorcycle', 'Motorcycle'), ('car', 'Car'), ('bicycle', 'Bicycle')], max_length=30)),
                ('vehicle_make_model', models.CharField(blank=True, max_length=120)),
                ('vehicle_year', models.PositiveIntegerField(blank=True, null=True)),
                ('vehicle_color', models.CharField(blank=True, max_length=60)),
                ('plate_number', models.CharField(blank=True, max_length=40)),
                ('available_days', models.CharField(blank=True, max_length=120)),
                ('shift_start', models.TimeField(blank=True, null=True)),
                ('shift_end', models.TimeField(blank=True, null=True)),
                ('coverage_areas', models.TextField(blank=True)),
                ('max_radius_km', models.PositiveIntegerField(default=5)),
                ('accepts_cod', models.BooleanField(default=True)),
                ('allows_multiple_dropoffs', models.BooleanField(default=True)),
                ('nearby_job_alerts', models.BooleanField(default=True)),
                ('government_id_front', models.FileField(blank=True, null=True, upload_to='rider_docs/')),
                ('government_id_back', models.FileField(blank=True, null=True, upload_to='rider_docs/')),
                ('drivers_license_front', models.FileField(blank=True, null=True, upload_to='rider_docs/')),
                ('drivers_license_back', models.FileField(blank=True, null=True, upload_to='rider_docs/')),
                ('vehicle_cr', models.FileField(blank=True, null=True, upload_to='rider_docs/')),
                ('vehicle_or', models.FileField(blank=True, null=True, upload_to='rider_docs/')),
                ('selfie_with_id', models.FileField(blank=True, null=True, upload_to='rider_docs/')),
                ('is_submitted', models.BooleanField(default=False)),
                ('submitted_at', models.DateTimeField(blank=True, null=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='rider_profile', to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
