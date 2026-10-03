from django import forms

from .models import Promotion


class PromotionForm(forms.ModelForm):
    class Meta:
        model = Promotion
        fields = ('title', 'description', 'discount_type', 'discount_value', 'min_order_amount',
                  'start_date', 'end_date', 'is_active')
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3, 'maxlength': 500}),
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'end_date': forms.DateInput(attrs={'type': 'date'}),
            'discount_value': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'min_order_amount': forms.NumberInput(attrs={'step': '0.01', 'min': '0', 'placeholder': 'Optional'}),
        }
        labels = {
            'discount_value': 'Discount value',
            'min_order_amount': 'Minimum order (₱)',
            'is_active': 'Show this promo to customers',
        }

    def clean_title(self):
        title = self.cleaned_data['title'].strip()
        if not title:
            raise forms.ValidationError('Title is required.')
        return title
