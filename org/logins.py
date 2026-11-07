"""Create and reset staff logins. Used by the seed command and the Staff logins page."""
import secrets

from django.contrib.auth import get_user_model


def username_for(staff):
    return f"{staff.first_name}.{staff.last_name}".lower().replace(" ", "").replace("'", "")


def temp_password():
    # 12 characters, easy to read out over the phone (no 0/O, 1/l/I).
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def create_login(staff, superuser=False):
    """Give a staff member a login. Returns (username, temporary password)."""
    User = get_user_model()
    username = username_for(staff)
    base, n = username, 2
    while User.objects.filter(username=username).exists():
        username = f"{base}{n}"
        n += 1
    password = temp_password()
    user = User.objects.create_user(
        username=username,
        password=password,
        first_name=staff.first_name,
        last_name=staff.last_name,
        email=staff.email,
    )
    if superuser:
        user.is_staff = user.is_superuser = True
        user.save()
    staff.user = user
    staff.save(update_fields=["user"])
    return username, password


def reset_password(staff):
    password = temp_password()
    staff.user.set_password(password)
    staff.user.save()
    return staff.user.username, password
