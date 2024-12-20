from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
import os
import pandas as pd
import datetime
import firebase_admin
from firebase_admin import credentials, firestore, auth
from google.cloud import firestore
from werkzeug.utils import secure_filename

# Initialize Flask App
app = Flask(__name__)
app.secret_key = 'your_secret_key'

# Initialize Firebase
cred = credentials.Certificate("firebase-key.json")
firebase_admin.initialize_app(cred)
db = firestore.Client()

# Ensure uploads directory exists
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'csv', 'xls', 'xlsx'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def is_authenticated():
    return 'user' in session and 'role' in session

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.before_request
def redirect_if_not_authenticated():
    # If the user is trying to access any page before logging in, redirect them to the login page
    if not is_authenticated() and request.endpoint not in ['login', 'google_login', 'static']:
        return redirect(url_for('login'))

@app.route('/')
def home():
    if not is_authenticated():
        return redirect(url_for('login'))
    # If authenticated, redirect to a page based on the role or show a dashboard
    role = session.get('role')
    if role == 'teacher':
        return redirect(url_for('teachers_home'))
    elif role == 'student':
        return redirect(url_for('students_home'))
    return redirect(url_for('login'))

@app.route('/google-login', methods=['POST'])
def google_login():
    try:
        data = request.get_json()
        token = data.get('token')

        # Verify the Firebase ID token
        decoded_token = auth.verify_id_token(token)
        user_email = decoded_token['email']
        print(f"User email being used: '{user_email}'")  # Debugging line

        user_doc = db.collection('users').document(user_email).get()
        if not user_doc.exists:
            return jsonify({'success': False, 'message': 'User not found in the database.'})

        role = user_doc.to_dict().get('role')
        session['user'] = user_email
        session['role'] = role

        return jsonify({'success': True, 'role': role})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']

        try:
            # Fetch user by email
            user = auth.get_user_by_email(email)

            # Check if user exists in Firestore
            user_doc = db.collection('users').document(email).get()
            if user_doc.exists:
                session['user'] = email
                session['role'] = user_doc.to_dict().get('role')  # Storing role in session
                if session['role'] == 'teacher':
                    flash(f'Welcome, {email}!')
                    return redirect(url_for('teachers_home'))
                else:
                    flash('Access denied! Only teachers can log in.')
            else:
                flash('User not found in the database. Please register first.')

        except Exception as e:
            flash(f'Login failed: {e}')

    return render_template('login.html')


@app.route('/teachers_home')
def teachers_home():
    if not is_authenticated() or session.get('role') != 'teacher':
        return redirect(url_for('login'))

    teacher_email = session['user']
    classrooms_ref = db.collection('classrooms').where('teacherEmail', '==', teacher_email).stream()
    classrooms = {doc.id: doc.to_dict() for doc in classrooms_ref}
    return render_template('teachers_home.html', classrooms=classrooms)
    
@app.route('/students_home')
def students_home():
    if not is_authenticated() or session.get('role') != 'student':
        return redirect(url_for('login'))

    student_email = session['user']
    classrooms_ref = db.collection('classrooms').stream()
    classrooms = {}
    for classroom_doc in classrooms_ref:
        students_ref = classroom_doc.reference.collection('students').document(student_email).get()
        if students_ref.exists:
            projects_ref = classroom_doc.reference.collection('projects').stream()
            projects = {proj.id: proj.to_dict() for proj in projects_ref}
            classrooms[classroom_doc.id] = projects

    return render_template('students_home.html', classrooms=classrooms)


@app.route('/logout')
def logout():
    session.pop('user', None)
    flash('You have been logged out.')
    return redirect(url_for('login'))


@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if not is_authenticated() or session['role'] != 'teacher':
        return redirect(url_for('login'))

    if request.method == 'POST':
        class_name = request.form['class_name']
        file = request.files['student_file']

        if not class_name or not file:
            flash('Class name and file are required.')
            return redirect(url_for('upload'))

        file_ext = os.path.splitext(file.filename)[1].lower()
        if file_ext not in ['.csv', '.xlsx']:
            flash('Only CSV or Excel files are allowed.')
            return redirect(url_for('upload'))

        file_path = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(file_path)

        try:
            if file_ext == '.csv':
                df = pd.read_csv(file_path)
            else:
                df = pd.read_excel(file_path)

            if not {'firstname', 'lastname', 'email'}.issubset(df.columns):
                flash('File must have columns: firstname, lastname, email.')
                return redirect(url_for('upload'))

            classroom_ref = db.collection('classrooms').document(class_name)
            classroom_ref.set({'classID': class_name, 'teacherEmail': session['user']})

            for _, row in df.iterrows():
                student_email = row['email']
                student_data = {
                    'firstName': row['firstname'],
                    'lastName': row['lastname'],
                    'email': student_email,
                    'assignedAt': firestore.SERVER_TIMESTAMP
                }
                classroom_ref.collection('students').document(student_email).set(student_data)
                
                user_doc = db.collection('users').document(student_email)
                if not user_doc.get().exists:
                    user_doc.set({
                        'email': student_email,
                        'role': 'student',
                        'name': f"{row['lastname']}, {row['firstname']}",
                        'createdAt': firestore.SERVER_TIMESTAMP
                    })

            flash(f'Classroom "{class_name}" created successfully!')
            return redirect(url_for('teachers_home'))

        except Exception as e:
            flash(f'Error processing file: {e}')
            return redirect(url_for('upload'))

        finally:
            os.remove(file_path)

    return render_template('upload.html')

@app.route('/classroom/<class_name>')
def classroom_view(class_name):
    if not is_authenticated():
        return redirect(url_for('login'))

    classroom_ref = db.collection('classrooms').document(class_name).get()
    if not classroom_ref.exists:
        flash("Classroom not found.")
        return redirect(url_for('teachers_home'))

    classroom = classroom_ref.to_dict()
    teacher_email = classroom['teacherEmail']
    student_emails = [s.id for s in db.collection('classrooms').document(class_name).collection('students').stream()]

    if session['user'] != teacher_email and session['user'] not in student_emails:
        flash("You do not have access to this classroom.")
        return redirect(url_for('login'))

    projects_ref = db.collection('classrooms').document(class_name).collection('Projects').stream()
    projects = {proj.id: proj.to_dict() for proj in projects_ref}
    return render_template('classroom.html', class_name=class_name, projects=projects)

@app.route('/classroom/<class_name>/add_project', methods=['GET', 'POST'])
def add_project(class_name):
    if not is_authenticated():
        return redirect(url_for('login'))

    # Get the classroom reference
    classroom_ref = db.collection('classrooms').document(class_name).get()
    if not classroom_ref.exists or classroom_ref.to_dict()['teacherEmail'] != session['user']:
        flash("You do not have permission to add projects.")
        return redirect(url_for('login'))

    if request.method == 'POST':
        project_name = request.form['project_name']
        project_description = request.form['description']
        due_date_str = request.form['due_date']

        if not project_name:
            flash("Project name is required.")
            return redirect(url_for('add_project', class_name=class_name))

        # Convert the due date to a Firestore timestamp if it's provided
        if due_date_str:
            due_date = datetime.datetime.strptime(due_date_str, '%Y-%m-%d')
        else:
            due_date = None

        # Handle file upload
        team_file = request.files.get('team_file')
        if team_file and allowed_file(team_file.filename):
            filename = secure_filename(team_file.filename)
            file_path = os.path.join('uploads', filename)
            team_file.save(file_path)
            
            # Parse the CSV/Excel file to extract student information
            try:
                if filename.endswith('.csv'):
                    data = pd.read_csv(file_path)
                else:
                    data = pd.read_excel(file_path)

                # Validate the required columns
                required_columns = ['firstname', 'lastname', 'email', 'teamname']
                missing_columns = [col for col in required_columns if col not in data.columns]
                if missing_columns:
                    flash(f"Invalid file format. Missing columns: {', '.join(missing_columns)}.")
                    return redirect(url_for('add_project', class_name=class_name))

                # Check for missing or empty values in each row
                for idx, row in data.iterrows():
                    for col in required_columns:
                        # Ensure that the value is a string and not empty
                        value = row[col]
                        if pd.isna(value) or (not isinstance(value, str)) or value.strip() == "":
                            flash(f"Row {idx+1} has an invalid value in column '{col}'. All fields must be non-empty strings.")
                            return redirect(url_for('add_project', class_name=class_name))

                # Get list of student emails in the class
                class_students = [student.id for student in db.collection('classrooms').document(class_name).collection('students').stream()]

                # Check if the students in the file are part of the class
                invalid_emails = [email for email in data['email'] if email not in class_students]
                if invalid_emails:
                    flash(f"The following students are not in this class: {', '.join(invalid_emails)}.")
                    return redirect(url_for('add_project', class_name=class_name))
                
                # Update teams
                for _, row in data.iterrows():
                    team_name = row['teamname']
                    student_email = row['email']
                    student_name = f"{row['lastname']}, {row['firstname']}"

                    # Reference to the team's subcollection inside the project
                    team_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name).collection('teams').document(team_name)
                    
                    # Add the student to the team subcollection
                    team_ref.set({
                        student_email: student_name
                    }, merge=True)

            except Exception as e:
                flash(f"Error processing file: {str(e)}")
                return redirect(url_for('add_project', class_name=class_name))

        # Reference to the project's Firestore document
        project_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name)

        # Update project details in Firestore
        project_ref.set({
            'projectName': project_name,
            'description': project_description,  # Store the description
            'dueDate': due_date,  # Store the due date (as a timestamp)
            'createdAt': firestore.SERVER_TIMESTAMP,  # Store the creation timestamp
        })

        flash(f"Project '{project_name}' added to classroom {class_name}.")
        return redirect(url_for('classroom_view', class_name=class_name))

    return render_template('add_project.html', class_name=class_name)

@app.route('/classroom/<class_name>/project/<project_name>')
def project_view(class_name, project_name):
    if not is_authenticated():
        return redirect(url_for('login'))

    # Fetch the project document
    project_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name).get()
    if not project_ref.exists:
        flash("Project not found.")
        return redirect(url_for('classroom_view', class_name=class_name))

    project_data = project_ref.to_dict()

    # Fetch the classroom document
    classroom_ref = db.collection('classrooms').document(class_name).get()
    teacher_email = classroom_ref.to_dict().get('teacherEmail', '')
    student_emails = [s.id for s in db.collection('classrooms').document(class_name).collection('students').stream()]

    # Check if the user has access to the project
    if session['user'] != teacher_email and session['user'] not in student_emails:
        flash("You do not have access to this project.")
        return redirect(url_for('login'))

    # Fetch teams for the project
    teams_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name).collection('teams').stream()
    teams = {team.id: team.to_dict() for team in teams_ref}

    # Check if the student is assigned to any team
    user_role = session.get('role')
    student_team_assigned = None
    if user_role == 'student' and session['user'] in student_emails:
        for team_name, team_members in teams.items():
            if session['user'] in team_members:
                student_team_assigned = team_name
                break

    return render_template(
        'project.html',
        class_name=class_name,
        project_name=project_name,
        project_description=project_data.get('description', 'No description provided'),
        due_date=project_data.get('dueDate', 'Not set'),
        created_at=project_data.get('createdAt', 'Unknown'),
        teams=teams,
        student_team_assigned=student_team_assigned
    )

@app.route('/classroom/<class_name>/project/<project_name>/add_team', methods=['GET', 'POST'])
def add_team(class_name, project_name):
    if not is_authenticated():
        return redirect(url_for('login'))

    classroom_ref = db.collection('classrooms').document(class_name).get()
    if not classroom_ref.exists or classroom_ref.to_dict()['teacherEmail'] != session['user']:
        flash("You do not have permission to add teams.")
        return redirect(url_for('login'))

    if request.method == 'POST':
        team_name = request.form['team_name']
        selected_students = request.form.getlist('students')

        if not team_name or not selected_students:
            flash('Team name and at least one student are required.')
            return redirect(url_for('add_team', class_name=class_name, project_name=project_name))

        # Get student details and ensure they exist in the class
        team_data = {}
        for student_email in selected_students:
            student_ref = db.collection('classrooms').document(class_name).collection('students').document(student_email).get()
            if student_ref.exists:
                student_data = student_ref.to_dict()
                team_data[student_email] = f"{student_data['lastName']}, {student_data['firstName']}"
            else:
                flash(f"Student {student_email} not found.")
                return redirect(url_for('add_team', class_name=class_name, project_name=project_name))

        # Create the team in Firestore
        team_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name).collection('teams').document(team_name)
        team_ref.set(team_data)

        flash(f'Team "{team_name}" created successfully!')
        return redirect(url_for('project_view', class_name=class_name, project_name=project_name))

    # Fetch all students who are not assigned to a team for this project
    all_students = db.collection('classrooms').document(class_name).collection('students').stream()
    assigned_students = set()
    teams_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name).collection('teams').stream()
    for team in teams_ref:
        assigned_students.update(team.to_dict().keys())

    available_students = [
        {'email': s.id, 'firstName': s.to_dict()['firstName'], 'lastName': s.to_dict()['lastName']} 
        for s in all_students if s.id not in assigned_students
    ]

    return render_template('add_team.html', class_name=class_name, project_name=project_name, students=available_students)

@app.route('/classroom/<class_name>/project/<project_name>/team/<team_name>')
def team_view(class_name, project_name, team_name):
    if not is_authenticated():
        return redirect(url_for('login'))

    team_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name).collection('teams').document(team_name).get()

    if not team_ref.exists:
        flash("Team not found.")
        return redirect(url_for('project_view', class_name=class_name, project_name=project_name))

    team = team_ref.to_dict()
    members_data = [f"{name}" for name in team.values()]

    # Verify student access
    if session['role'] == 'student' and session['user'] not in team:
        flash("You are not a member of this team.")
        return redirect(url_for('project_view', class_name=class_name, project_name=project_name))

    return render_template(
        'team.html', 
        class_name=class_name, 
        project_name=project_name, 
        team_name=team_name, 
        team_members=members_data
    )


if __name__ == '__main__':
    app.run(debug=True)