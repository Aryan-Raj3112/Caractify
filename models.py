from flask_login import UserMixin

class User(UserMixin):
    def __init__(self, user_id, email=None, name=None):
        self.id = user_id
        self.email = email
        self.name = name

    def is_authenticated(self):
        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False