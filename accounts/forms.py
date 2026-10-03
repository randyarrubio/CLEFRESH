import re

from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from .models import User


_PW_UPPER = re.compile(r'[A-Z]')
_PW_LOWER = re.compile(r'[a-z]')
_PW_DIGIT = re.compile(r'\d')
_PW_SYMBOL = re.compile(r'[^A-Za-z0-9]')


def validate_password_strength(pw):
    """Shared by registration and password reset."""
    if len(pw) < 8:
        raise forms.ValidationError('Password must be at least 8 characters.')
    if len(pw) > 128:
        raise forms.ValidationError('Password is too long.')
    missing = []
    if not _PW_UPPER.search(pw):
        missing.append('an uppercase letter')
    if not _PW_LOWER.search(pw):
        missing.append('a lowercase letter')
    if not _PW_DIGIT.search(pw):
        missing.append('a number')
    if not _PW_SYMBOL.search(pw):
        missing.append('a symbol')
    if missing:
        raise forms.ValidationError('Password must contain ' + ', '.join(missing) + '.')


class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=True)
    first_name = forms.CharField(max_length=50, required=True)
    last_name = forms.CharField(max_length=50, required=True)
    role = forms.ChoiceField(choices=[
        ('customer', 'Customer'),
        ('shop_owner', 'Shop Owner'),
        ('rider', 'Rider'),
    ])
    contact_number = forms.CharField(max_length=20, required=False)

    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email', 'role', 'contact_number', 'password1', 'password2')

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exists() or User.objects.filter(username__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email

    def clean_password1(self):
        pw = self.cleaned_data.get('password1') or ''
        validate_password_strength(pw)
        return pw

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = self.cleaned_data['email']
        user.email = self.cleaned_data['email']
        user.role = self.cleaned_data['role']
        if commit:
            user.save()
        return user


class LoginForm(AuthenticationForm):
    username = forms.EmailField(label='Email')


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'contact_number', 'address', 'profile_picture', 'email_notifications_enabled')
