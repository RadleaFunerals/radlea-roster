from django.urls import path

from . import views

app_name = "roster"

urlpatterns = [
    path("", views.home, name="home"),
    path("me/", views.my_roster, name="my_roster"),
    path("tonight/", views.tonight, name="tonight"),
    # Week roster per location
    path("week/<str:code>/", views.week_view, name="week_current"),
    path("week/<str:code>/<str:start>/", views.week_view, name="week"),
    path("week/<str:code>/<str:start>/fill-usual/", views.fill_usual, name="fill_usual"),
    path("week/<str:code>/<str:start>/copy-after-hours/", views.copy_oncall, name="copy_oncall"),
    path("week/<str:code>/<str:start>/publish/", views.publish_week, name="publish"),
    path("week/<str:code>/<str:start>/print/", views.print_week, name="print_week"),
    # Normal hours
    path("shift/new/<str:code>/", views.shift_create, name="shift_create"),
    path("shift/<int:pk>/", views.shift_edit, name="shift_edit"),
    path("shift/<int:pk>/delete/", views.shift_delete, name="shift_delete"),
    # After hours
    path("after-hours/<str:code>/<str:day>/", views.oncall_edit, name="oncall_edit"),
    # Gentle Way
    path("gentle-way/new/<str:code>/", views.gw_create, name="gw_create"),
    path("gentle-way/<int:pk>/", views.gw_edit, name="gw_edit"),
    path("gentle-way/<int:pk>/delete/", views.gw_delete, name="gw_delete"),
    path("staff-logins/", views.staff_logins, name="staff_logins"),
    # Staff requests
    path("requests/", views.requests_queue, name="requests"),
    path("requests/unavailable/new/", views.unavailability_new, name="unavailability_new"),
    path("requests/swap/new/", views.swap_new, name="swap_new"),
    path("requests/unavailable/<int:pk>/decide/", views.decide_unavailability, name="decide_unavailability"),
    path("requests/swap/<int:pk>/decide/", views.decide_swap, name="decide_swap"),
    path("requests/<str:kind>/<int:pk>/withdraw/", views.withdraw_request, name="withdraw"),
]
