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

from datetime import datetime

from datetime import datetime

@app.route('/classroom/<class_name>/add_project', methods=['GET', 'POST'])
def add_project(class_name):
    if not is_authenticated():
        return redirect(url_for('login'))

    # Get the classroom reference
    classroom_ref = db.collection('classrooms').document(class_name).get()
    if not classroom_ref.exists or classroom_ref.to_dict()['teacherEmail'] != session['user']:
        flash("You do not have permission to add projects.")
        return redirect(url_for('login'))

    # Get the current date and time to pass to the template
    current_date_time = datetime.now().strftime('%Y-%m-%dT%H:%M')

    if request.method == 'POST':
        project_name = request.form['project_name']
        project_description = request.form['description']
        due_date_str = request.form['due_date']

        if not project_name:
            flash("Project name is required.")
            return redirect(url_for('add_project', class_name=class_name))

        # Convert the due date to a Firestore timestamp if it's provided
        due_date = None
        if due_date_str:
            try:
                # Parse the due date and time
                due_date = datetime.strptime(due_date_str, '%Y-%m-%dT%H:%M')

                # Validate that the due date is in the future and the year is <= 9999
                if due_date <= datetime.now():
                    flash("Due date must be a future date and time.")
                    return redirect(url_for('add_project', class_name=class_name))

                if due_date.year > 9999:
                    flash("Year cannot be greater than 9999.")
                    return redirect(url_for('add_project', class_name=class_name))

            except ValueError:
                flash("Invalid date and time format. Please use YYYY-MM-DDTHH:MM.")
                return redirect(url_for('add_project', class_name=class_name))

        # Handle file upload for teams
        team_file = request.files.get('team_file')
        teams_created = False

        if team_file and allowed_file(team_file.filename):
            filename = secure_filename(team_file.filename)
            file_path = os.path.join('uploads', filename)
            team_file.save(file_path)

            try:
                # Parse the file to extract team details
                if filename.endswith('.csv'):
                    data = pd.read_csv(file_path)
                else:
                    data = pd.read_excel(file_path)

                # Validate required columns
                required_columns = ['firstname', 'lastname', 'email', 'teamname']
                if any(col not in data.columns for col in required_columns):
                    flash(f"File must include columns: {', '.join(required_columns)}.")
                    return redirect(url_for('add_project', class_name=class_name))

                class_students = [
                    student.id for student in db.collection('classrooms')
                    .document(class_name).collection('students').stream()
                ]

                # Verify student emails and prepare team data
                for _, row in data.iterrows():
                    student_email = row['email']
                    team_name = row['teamname']
                    student_name = f"{row['lastname']}, {row['firstname']}"

                    if student_email not in class_students:
                        flash(f"Student {student_email} is not part of this class.")
                        return redirect(url_for('add_project', class_name=class_name))

                    team_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name).collection('teams').document(team_name)
                    team_ref.set({
                        student_email: student_name
                    }, merge=True)

                teams_created = True
            except Exception as e:
                flash(f"Error processing team file: {str(e)}")
                return redirect(url_for('add_project', class_name=class_name))

        # Create the project document in Firestore
        project_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name)
        project_ref.set({
            'projectName': project_name,
            'description': project_description,
            'dueDate': due_date,
            'createdAt': firestore.SERVER_TIMESTAMP,
        })

        if teams_created:
            flash(f"Project '{project_name}' with teams added to classroom {class_name}.")
        else:
            flash(f"Project '{project_name}' added to classroom {class_name}. You can add teams later.")
        return redirect(url_for('classroom_view', class_name=class_name))

    return render_template('add_project.html', class_name=class_name, current_date_time=current_date_time)

from datetime import datetime

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

    # Format the due date to remove timezone and display in a readable format
    due_date = project_data.get('dueDate', None)
    if due_date:
        # Convert Firestore timestamp (with timezone) to a regular datetime string
        due_date = due_date.replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S')

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
        due_date=due_date,
        created_at=project_data.get('createdAt', 'Unknown'),
        teams=teams,
        student_team_assigned=student_team_assigned
    )

@app.route('/classroom/<class_name>/project/<project_name>/add_team', methods=['GET', 'POST'])
def add_team(class_name, project_name):
    if not is_authenticated():
        return redirect(url_for('login'))

    # Check teacher permission
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

        # Prevent duplicates in the same project
        teams_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name).collection('teams').stream()
        assigned_students = {student_email for team in teams_ref for student_email in team.to_dict().keys()}

        for student_email in selected_students:
            if student_email in assigned_students and student_email not in request.form.getlist(f'team_{team_name}_existing'):
                flash(f"Student {student_email} is already assigned to a team.")
                return redirect(url_for('add_team', class_name=class_name, project_name=project_name))

        # Update or create the team
        team_data = {}
        for student_email in selected_students:
            student_ref = db.collection('classrooms').document(class_name).collection('students').document(student_email).get()
            if student_ref.exists:
                student_data = student_ref.to_dict()
                team_data[student_email] = f"{student_data['lastName']}, {student_data['firstName']}"
            else:
                flash(f"Student {student_email} not found.")
                return redirect(url_for('add_team', class_name=class_name, project_name=project_name))

        team_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name).collection('teams').document(team_name)
        team_ref.set(team_data)

        flash(f'Team "{team_name}" updated successfully!')
        return redirect(url_for('add_team', class_name=class_name, project_name=project_name))

    # Fetch students and teams
    all_students = db.collection('classrooms').document(class_name).collection('students').stream()
    teams_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name).collection('teams').stream()
    
    available_students = []
    assigned_students = {}
    for s in all_students:
        student = s.to_dict()
        student_email = s.id
        assigned_students[student_email] = False
        available_students.append({'email': student_email, 'firstName': student['firstName'], 'lastName': student['lastName']})

    teams = []
    for team in teams_ref:
        team_name = team.id
        team_data = team.to_dict()
        students_in_team = [{'email': email, 'name': team_data[email]} for email in team_data]
        teams.append({'teamName': team_name, 'students': students_in_team})
        for student in students_in_team:
            assigned_students[student['email']] = True

    # Filter available students
    available_students = [s for s in available_students if not assigned_students[s['email']]]

    return render_template(
        'add_team.html',
        class_name=class_name,
        project_name=project_name,
        students=available_students,
        teams=teams
    )

@app.route('/save-teams', methods=['POST'])
def save_teams():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data received"}), 400
        
        teams = data.get("teams", [])
        class_name = data.get("class_name")
        project_name = data.get("project_name")

        if not class_name or not project_name:
            return jsonify({"error": "Class name or project name missing"}), 400

        # Reference to the project's teams collection in Firestore
        teams_collection_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name).collection('teams')

        # Fetch existing teams from Firestore
        existing_teams = teams_collection_ref.stream()
        existing_team_data = {team.id: team.to_dict() for team in existing_teams}

        # Track all processed students to avoid duplication
        processed_students = set()

        for team in teams:
            team_name = team.get("teamName")
            students = [s for s in team.get("students", []) if s != "No students"]

            # Skip saving empty teams
            if not team_name or not students:
                continue

            # Prepare team data
            team_data = {}
            for student_email in students:
                # Ensure the student is not already processed
                if student_email in processed_students:
                    continue

                processed_students.add(student_email)

                # Fetch student details
                student_ref = db.collection('classrooms').document(class_name).collection('students').document(student_email).get()
                if student_ref.exists:
                    student_info = student_ref.to_dict()
                    team_data[student_email] = f"{student_info['lastName']}, {student_info['firstName']}"

                    # Remove the student from their old team
                    for old_team_name, old_team_data in existing_team_data.items():
                        if student_email in old_team_data:
                            del existing_team_data[old_team_name][student_email]

                            # If the old team becomes empty, delete it
                            if not existing_team_data[old_team_name]:
                                teams_collection_ref.document(old_team_name).delete()
                            else:
                                teams_collection_ref.document(old_team_name).set(existing_team_data[old_team_name])
                else:
                    return jsonify({"error": f"Student {student_email} does not exist in the classroom"}), 400
            
            # Save the new team data
            teams_collection_ref.document(team_name).set(team_data)

        return jsonify({"status": "success", "message": "Teams saved successfully!"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


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