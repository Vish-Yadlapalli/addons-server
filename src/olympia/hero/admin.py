from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms import BaseInlineFormSet
from django.utils.safestring import mark_safe

from .models import (
    PrimaryHero, SecondaryHeroModule,
    PrimaryHeroImage)


class ImageChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return mark_safe(
            '<img src="{}" width="150" height="120" />'.format(
                obj.custom_image.url))


class PrimaryHeroInline(admin.StackedInline):
    model = PrimaryHero
    fields = (
        'select_image',
        'gradient_color',
        'is_external',
        'enabled')
    view_on_site = False
    can_delete = False

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'select_image':
            kwargs['required'] = False
            kwargs['widget'] = forms.RadioSelect(attrs={
                'class': 'inline',
                'style': 'vertical-align: top'
            })
            kwargs['queryset'] = PrimaryHeroImage.objects
            kwargs['empty_label'] = mark_safe("""
                <div style="width: 147px; display: inline-block">
                    No image selected
                </div>
                """)
            return ImageChoiceField(**kwargs)
        return super().formfield_for_foreignkey(
            db_field, request, **kwargs)


class PrimaryHeroImageAdmin(admin.ModelAdmin):
    list_display = ('preview_image',)
    actions = ['delete_selected']
    readonly_fields = ('preview_image',)


class HeroModuleInlineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if len(self.forms) != 3:
            raise ValidationError(
                'There must be exactly 3 modules in this shelf.')


class SecondaryHeroModuleInline(admin.StackedInline):
    model = SecondaryHeroModule
    view_on_site = False
    max_num = 3
    min_num = 3
    can_delete = False
    formset = HeroModuleInlineFormSet


class SecondaryHeroAdmin(admin.ModelAdmin):
    class Media:
        css = {
            'all': ('css/admin/discovery.css',)
        }
    list_display = ('headline', 'description', 'enabled')
    inlines = [SecondaryHeroModuleInline]
    view_on_site = False

    def has_delete_permission(self, request, obj=None):
        qs = self.get_queryset(request).filter(enabled=True)
        if obj and list(qs) == [obj]:
            return False
        return super().has_delete_permission(request=request, obj=obj)
