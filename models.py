from flask_login import UserMixin

class User(UserMixin):
    def __init__(self, user_id):
        self.id = user_id

    def is_authenticated(self):
        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False