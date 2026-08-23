# Deliberately empty. These models are platform-owned commercial state with no
# writer yet (Step 3B is schema only), and `django.contrib.admin` is not even
# routed — dinify_backend/urls.py has the admin include commented out. Registering
# them here would manufacture an unaudited write surface for exactly the facts a
# future service is meant to write behind elevation, a reason and an audit row.
