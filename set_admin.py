"""
Usage:  python set_admin.py your@email.com
Run inside the web container:
  docker exec -it whatsapp-saas-web-1 python set_admin.py your@email.com
"""
import sys
from app import create_app, db
from app.models import User

def main():
    if len(sys.argv) < 2:
        print("Usage: python set_admin.py <email>")
        sys.exit(1)

    email = sys.argv[1].strip().lower()
    app = create_app()
    with app.app_context():
        user = User.query.filter_by(email=email).first()
        if not user:
            print(f"User not found: {email}")
            sys.exit(1)
        user.is_admin = True
        db.session.commit()
        print(f"✓ {email} is now admin")

if __name__ == '__main__':
    main()
