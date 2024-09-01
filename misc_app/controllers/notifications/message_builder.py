footer = """
    <p><span style="font-weight: 400;">
    Thank you for choosing Dinify.
    &nbsp;</span></p>
    <br><p></p>

    <p><span style="font-weight: 400;">Sincerely,&nbsp;</span></p>
    <p><span style="font-weight: 400;">Various Links to Dinify Social Media Accounts</span></p>
"""


def build_messages(msg_data: dict) -> dict:
    """
    Build the messages to be sent to the user
    """
    message = {
        'email': None,
        'sms': None
    }

    if msg_data.get('msg_type') == 'new-restaurant':
        email = f"""
        <p><span style="font-weight: 400;">Hello {msg_data['first_name']},</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        Your application for the restaurant, {msg_data['restaurant_name']}, has been submitted to Dinify.&nbsp;</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        We shall confirm the details and get back to you as soon as possible.&nbsp;</span></p>
        </span></p>
        <br><p></p>

        {footer}
        """
        message = {
            'email': email,
            'sms': None
        }

    elif msg_data.get('msg_type') == 'admin-new-restaurant':
        email = f"""
        <p><span style="font-weight: 400;">Hello {msg_data['first_name']},</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        A profile for your restaurant, {msg_data['restaurant_name']}, has created on Dinify.&nbsp;</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        We shall confirm the details and get back to you as soon as possible.&nbsp;</span></p>
        </span></p>
        <br><p></p>
        {footer}
        """
        message = {
            'email': email,
            'sms': None
        }

    elif msg_data.get('msg_type') == 'new-restaurant-employee':
        email = f"""
        <p><span style="font-weight: 400;">Hello {msg_data['first_name']},</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        You have been granted access to the restaurant, {msg_data['restaurant_name']} on Dinify.&nbsp;</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        You can now access the restaurant's account and perform your respective duties.&nbsp;</span></p>
        </span></p>
        <br><p></p>
        {footer}
        """
        message = {
            'email': email,
            'sms': None
        }

    elif msg_data.get('msg_type') == 'password-change':
        email = f"""
        <p><span style="font-weight: 400;">Hello {msg_data['first_name']},</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        Your Dinify password has been changed.&nbsp;</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        If you made this change, you are all set.&nbsp;</span></p>
        </span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        Didn&rsquo;t reset your password? Please take these steps to secure your account.&nbsp;</span></p>
        </span></p>
        <br><p></p>
        {footer}
        """
        message = {
            'email': email,
            'sms': None
        }

    elif msg_data.get('msg_type') == 'forgot-password':
        email = f"""
        <p><span style="font-weight: 400;">Hello {msg_data['first_name']},</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        Your One-Time Dinify password is {msg_data['password']}.&nbsp;</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        Didn&rsquo;t reset your password? Please take these steps to secure your account.&nbsp;</span></p>
        </span></p>
        <br><p></p>
        {footer}
        """
        message = {
            'email': email,
            'sms': None
        }

    return message
