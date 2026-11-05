from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html

from .models import Invitation, Membership, Organization, add_organization_member

User = get_user_model()


admin.site.unregister(User)


@admin.register(User)
class VimpleUserAdmin(UserAdmin):
    def get_list_display(self, request):
        display = super().get_list_display(request)
        if request.user.is_superuser:
            return (*display, "open_as_user")
        return display

    @admin.display(description="Open")
    def open_as_user(self, obj):
        if obj.is_superuser or not obj.is_active:
            return ""
        url = reverse("impersonate", args=[obj.pk])
        return format_html('<a href="{}">Open as this user</a>', url)


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "id", "created_at")
    search_fields = ("name", "id")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "role")
    list_filter = ("role",)
    search_fields = ("user__email", "user__username", "organization__name")


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "organization", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("email", "organization__name")

    def save_model(self, request, obj, form, change):
        outcome = add_organization_member(obj.organization, obj.email, obj.role)
        if isinstance(outcome, Invitation):
            obj.pk = outcome.pk
            obj.email = outcome.email
            obj.role = outcome.role
            obj._linked_user = False
        else:
            obj._linked_user = True

    def log_addition(self, request, obj, message):
        if getattr(obj, "_linked_user", False):
            return
        super().log_addition(request, obj, message)

    def log_change(self, request, obj, message):
        if getattr(obj, "_linked_user", False):
            return
        super().log_change(request, obj, message)

    def response_add(self, request, obj, post_url_continue=None):
        if getattr(obj, "_linked_user", False):
            self.message_user(
                request,
                "That person is now a member of the organization.",
                messages.SUCCESS,
            )
            return redirect("admin:accounts_membership_changelist")
        return super().response_add(request, obj, post_url_continue)

    def response_change(self, request, obj):
        if getattr(obj, "_linked_user", False):
            self.message_user(
                request,
                "That person is now a member of the organization.",
                messages.SUCCESS,
            )
            return redirect("admin:accounts_membership_changelist")
        return super().response_change(request, obj)
