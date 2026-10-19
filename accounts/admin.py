"""Admin registration for the custom user model."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """Django's UserAdmin plus the LMS role column, filter and form field."""

    list_display = [*BaseUserAdmin.list_display, "role"]
    list_filter = [*BaseUserAdmin.list_filter, "role"]
    fieldsets = (
        *BaseUserAdmin.fieldsets,
        (_("LMS"), {"fields": ("role",)}),
    )
    add_fieldsets = (
        *BaseUserAdmin.add_fieldsets,
        (_("LMS"), {"fields": ("role",)}),
    )
