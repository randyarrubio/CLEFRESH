from django.db import models
from django.conf import settings


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    link = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Bell + stream snapshot on every page: latest 20 for a user, newest first.
            models.Index(fields=['user', '-created_at'], name='notif_user_created_idx'),
            # Unread badge count on every page.
            models.Index(fields=['user', 'is_read'], name='notif_user_unread_idx'),
        ]

    def __str__(self):
        return f'Notification for {self.user.email}: {self.message[:50]}'
