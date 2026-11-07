"""
Create Robin's director login on first deploy, so no command line is needed.

Reads the password from the RADLEA_DIRECTOR_PASSWORD environment variable.
Does nothing if Robin already has a login, so it is safe on every deploy and
never resets a password that has since been changed.
"""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from org.models import StaffMember


class Command(BaseCommand):
    help = "Create the first director login from RADLEA_DIRECTOR_PASSWORD."

    def handle(self, *args, **opts):
        password = os.environ.get("RADLEA_DIRECTOR_PASSWORD", "")
        username = os.environ.get("RADLEA_DIRECTOR_USERNAME", "robin.drury")
        staff = StaffMember.objects.filter(first_name="Robin", last_name="Drury").first()
        User = get_user_model()
        if staff is None or staff.user_id or User.objects.filter(username=username).exists():
            self.stdout.write("Director login already exists.")
            return
        if len(password) < 10:
            self.stdout.write(self.style.WARNING("RADLEA_DIRECTOR_PASSWORD not set (10+ characters): no director login created."))
            return
        user = User.objects.create_user(
            username=username, password=password, first_name="Robin", last_name="Drury", email=staff.email
        )
        user.is_staff = user.is_superuser = True
        user.save()
        staff.user = user
        staff.save(update_fields=["user"])
        self.stdout.write(self.style.SUCCESS(f"Director login '{username}' created."))
