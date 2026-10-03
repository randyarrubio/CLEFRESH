from django.db import migrations, models


def mark_existing_users_verified(apps, schema_editor):
    """Existing users were created before email verification existed; grandfather them in."""
    User = apps.get_model('accounts', 'User')
    User.objects.update(email_verified=True)


def revert_existing_users(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    User.objects.update(email_verified=False)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_alter_riderprofile_drivers_license_back_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='email_verified',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(mark_existing_users_verified, revert_existing_users),
    ]
