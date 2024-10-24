def make_menu_item_messages(msg_data, footer) -> dict:
    message = {
        'email': None,
        'sms': None,
        'subject': None
    }

    if msg_data.get('msg_type') == 'new-table':
        email = f"""
        <p><span style="font-weight: 400;">Hello {msg_data['restaurant_name']},</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        {msg_data['user']} has added a new table, {msg_data['table_number']}.&nbsp;</span></p>
        <br><p></p>
        {footer}
        """
        message = {
            'email': email,
            'sms': None
        }

    elif msg_data.get('msg_type') == 'table-updated':
        email = f"""
        <p><span style="font-weight: 400;">Hello {msg_data['restaurant_name']},</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        {msg_data['user']} has updated the details for the table, {msg_data['table_number']}.&nbsp;</span></p>
        <br><p></p>
        {footer}
        """
        message = {
            'email': email,
            'sms': None
        }

    elif msg_data.get('msg_type') == 'table-disabled':
        email = f"""
        <p><span style="font-weight: 400;">Hello {msg_data['restaurant_name']},</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        {msg_data['user']} has disabled the table, {msg_data['table_number']}.&nbsp;</span></p>
        <br><p></p>
        {footer}
        """
        message = {
            'email': email,
            'sms': None
        }

    elif msg_data.get('msg_type') == 'table-enabled':
        email = f"""
        <p><span style="font-weight: 400;">Hello {msg_data['restaurant_name']},</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        {msg_data['user']} has enabled the table, {msg_data['table_number']}.&nbsp;</span></p>
        <br><p></p>
        {footer}
        """
        message = {
            'email': email,
            'sms': None
        }

    elif msg_data.get('msg_type') == 'table-deleted':
        email = f"""
        <p><span style="font-weight: 400;">Hello {msg_data['restaurant_name']},</span></p>
        <br><p></p>

        <p><span style="font-weight: 400;">
        {msg_data['user']} has deleted the table, {msg_data['table_number']}.&nbsp;</span></p>
        <p><span style="font-weight: 400;">
        Reason for deleting: {msg_data['reason']}.&nbsp;</span></p>
        <br><p></p>
        {footer}
        """
        message = {
            'email': email,
            'sms': None
        }

    return message
