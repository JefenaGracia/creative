from flask import Flask, flash, get_flashed_messages, render_template, request, redirect, url_for, flash, session, jsonify
import os
import pandas as pd
import datetime
import firebase_admin
from datetime import datetime
from firebase_admin import credentials, firestore, auth
from google.cloud import firestore
from werkzeug.utils import secure_filename
import re

# Initialize Flask App
app = Flask(__name__)
app.secret_key = 'secret_key'

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
        print(f"User email being used: '{user_email}'") 

        user_doc = db.collection('users').document(user_email).get()
        if not user_doc.exists:
            flash('User not found in the database.', 'danger')  
            return jsonify({'success': False, 'message': 'User not found in the database.'})

        role = user_doc.to_dict().get('role')
        session['user'] = user_email
        session['role'] = role
        flash(f'Welcome to Creative Assistant', 'success') 
        return jsonify({'success': True, 'role': role})

    except Exception as e:
        flash(f'An error occurred during login: {e}', 'danger')  
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
                session['role'] = user_doc.to_dict().get('role') 
                if session['role'] == 'teacher':
                    return redirect(url_for('teachers_home'))
            else:
                flash('User not found in the database.', 'warning')  # Flashing warning message

        except Exception as e:
            flash(f'Login failed: {e}', 'danger')

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
        class_name = request.form.get('class_name', '').strip()
        file = request.files.get('student_file')

        # Improved Validation
        if not class_name:
            flash('Class name is required.', 'error')
            return redirect(url_for('upload'))

        if not file or file.filename == '':
            flash('File is required.', 'error')
            return redirect(url_for('upload'))

        file_ext = os.path.splitext(file.filename)[1].lower()
        if file_ext not in ['.csv', '.xlsx']:
            flash('Only CSV or Excel files are allowed.', 'error')
            return redirect(url_for('upload'))

        # Save file temporarily
        file_path = os.path.join(UPLOAD_FOLDER, file.filename)
        try:
            if not os.path.exists(UPLOAD_FOLDER):
                os.makedirs(UPLOAD_FOLDER)

            file.save(file_path)

            # Load data into a DataFrame
            if file_ext == '.csv':
                df = pd.read_csv(file_path)
            else:
                df = pd.read_excel(file_path)

            # Validate required columns
            required_columns = {'firstname', 'lastname', 'email'}
            if not required_columns.issubset(df.columns):
                flash(f'File must contain columns: {", ".join(required_columns)}.', 'danger')
                return redirect(url_for('upload'))

            # Ensure email column contains valid email addresses
            if not all(df['email'].apply(lambda x: isinstance(x, str) and '@' in x)):
                flash('All entries in the email column must have a valid email addresses.', 'danger')
                return redirect(url_for('upload'))

            # Add classroom and students to Firestore
            classroom_ref = db.collection('classrooms').document(class_name)
            classroom_ref.set({'classID': class_name, 'teacherEmail': session['user']})

            for _, row in df.iterrows():
                student_email = row['email'].strip()
                student_data = {
                    'firstName': row['firstname'].strip(),
                    'lastName': row['lastname'].strip(),
                    'email': student_email,
                    'assignedAt': firestore.SERVER_TIMESTAMP
                }
                classroom_ref.collection('students').document(student_email).set(student_data)

                user_doc = db.collection('users').document(student_email)
                if not user_doc.get().exists:
                    user_doc.set({
                        'email': student_email,
                        'role': 'student',
                        'name': f"{row['lastname'].strip()}, {row['firstname'].strip()}",
                        'createdAt': firestore.SERVER_TIMESTAMP
                    })

            flash(f'Classroom "{class_name}" created successfully!', 'success')
            return redirect(url_for('teachers_home'))

        except Exception as e:
            flash(f'Error processing file: {str(e)}', 'danger')
            return redirect(url_for('upload'))

        finally:
            if os.path.exists(file_path):
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

@app.route('/classroom/<class_name>/manage_students')
def manage_students(class_name):
    # Fetch students from the Firestore subcollection under classrooms
    classroom_ref = db.collection('classrooms').document(class_name)
    students_ref = classroom_ref.collection('students')
    students = students_ref.stream()
    
    student_list = []
    for student in students:
        student_data = student.to_dict()
        student_list.append({
            'id': student.id,
            'first_name': student_data['firstName'],
            'last_name': student_data['lastName'],
            'email': student_data['email']
        })

    return render_template('manage_students.html', students=student_list, class_name=class_name)

@app.route('/classroom/<class_name>/edit_student/<student_id>', methods=['GET', 'POST'])
def edit_student(class_name, student_id):
    # Get the student document from Firestore
    student_ref = db.collection('classrooms').document(class_name).collection('students').document(student_id)
    student_doc = student_ref.get()

    if not student_doc.exists:
        flash('Student not found.')
        return redirect(url_for('manage_students', class_name=class_name))

    student = student_doc.to_dict()

    # Check if the keys are correct
    print(student)  # Debugging line to check if the student data is loaded correctly

    if request.method == 'POST':
        # Get the updated values from the form
        first_name = request.form['first_name']
        last_name = request.form['last_name']
        email = request.form['email']

        # Update the student's information in the classroom's students subcollection
        student_ref.update({
            'firstName': first_name,
            'lastName': last_name,
            'email': email
        })

        # Also update the student's information in the 'users' collection
        user_doc = db.collection('users').document(student_id)
        user_doc.update({
            'name': f"{last_name}, {first_name}",
            'email': email
        })

        flash('Student information updated successfully.','success')
        return redirect(url_for('manage_students', class_name=class_name))

    return render_template('edit_student.html', student=student, class_name=class_name)

def sanitize_email(email):
    """Sanitize email by replacing non-alphanumeric characters."""
    return email.replace('@', '_at_').replace('.', '_dot_')

@app.route('/classroom/<class_name>/delete_student/<student_id>', methods=['POST'])
def delete_student(class_name, student_id):
    try:
        classroom_ref = db.collection('classrooms').document(class_name)
        student_ref = classroom_ref.collection('students').document(student_id)

        # Check if the student exists before trying to delete
        if not student_ref.get().exists:
            print(f"Student {student_id} not found.")
            flash('Student not found in the classroom', 'danger')
            return redirect(url_for('manage_students', class_name=class_name))
        
        # Delete the student from Firestore
        student_ref.delete()

        # Remove the student from all teams in the Projects collection
        projects_ref = classroom_ref.collection('Projects')
        projects = projects_ref.stream()

        for project in projects:
            project_ref = projects_ref.document(project.id)
            teams_ref = project_ref.collection('teams')

            # Iterate through all teams and remove the student from any team they are part of
            for team in teams_ref.stream():
                team_ref = teams_ref.document(team.id)
                team_data = team_ref.get().to_dict()

                # Sanitize the student_id email before using it as a field name
                sanitized_student_id = sanitize_email(student_id)

                # Check if the sanitized student_id is part of the team
                if sanitized_student_id in team_data:
                    # Remove the student from the team
                    team_ref.update({
                        sanitized_student_id: firestore.DELETE_FIELD
                    })

                    # If no students are left in the team, delete the team
                    if not team_ref.get().to_dict():
                        team_ref.delete()

        # Flash success message
        flash('Student has been successfully removed from the classroom', 'success')
        return redirect(url_for('manage_students', class_name=class_name))
    
    except Exception as e:
        flash(f'Error deleting student: {str(e)}', 'danger')
        return redirect(url_for('manage_students', class_name=class_name))

@app.route('/classroom/<class_name>/add_student', methods=['GET', 'POST'])
def add_student(class_name):
    if request.method == 'POST':
        first_name = request.form['first_name']
        last_name = request.form['last_name']
        email = request.form['email']

        classroom_ref = db.collection('classrooms').document(class_name)

        # Add student to the Firestore classroom students subcollection
        classroom_ref.collection('students').document(email).set({
            'firstName': first_name,
            'lastName': last_name,
            'email': email,
            'assignedAt': firestore.SERVER_TIMESTAMP
        })

        # Check if the student already exists in the users collection
        user_doc = db.collection('users').document(email)
        if not user_doc.get().exists:
            # If not, add the student to the 'users' collection
            user_doc.set({
                'email': email,
                'role': 'student',
                'name': f"{last_name}, {first_name}",
                'createdAt': firestore.SERVER_TIMESTAMP
            })
        flash(f'{first_name} {last_name} has been added to the classroom.', 'success')
        return redirect(url_for('manage_students', class_name=class_name))

    return render_template('add_student.html', class_name=class_name)

@app.route('/classroom/<class_name>/add_project', methods=['GET', 'POST'])
def add_project(class_name):
    if not is_authenticated():
        flash("Please log in to continue.", "danger")
        return redirect(url_for('login'))

    classroom_ref = db.collection('classrooms').document(class_name).get()
    if not classroom_ref.exists or classroom_ref.to_dict()['teacherEmail'] != session['user']:
        flash("You do not have permission to add projects.", "danger")
        return redirect(url_for('login'))

    current_date_time = datetime.now().strftime('%Y-%m-%dT%H:%M')

    if request.method == 'POST':
        project_name = request.form['project_name']
        project_description = request.form['description']
        due_date_str = request.form['due_date']

        if not project_name:
            flash("Project name is required.", "warning")
            return redirect(url_for('add_project', class_name=class_name))

        due_date = None
        if due_date_str:
            try:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%dT%H:%M')
                if due_date <= datetime.now():
                    flash("Due date must be in the future.", "warning")
                    return redirect(url_for('add_project', class_name=class_name))
                if due_date.year > 9999:
                    flash("Year cannot exceed 9999.", "warning")
                    return redirect(url_for('add_project', class_name=class_name))
            except ValueError:
                flash("Invalid date and time format. Use YYYY-MM-DDTHH:MM.", "danger")
                return redirect(url_for('add_project', class_name=class_name))

        team_file = request.files.get('team_file')
        teams_created = False

        if team_file and allowed_file(team_file.filename):
            filename = secure_filename(team_file.filename)
            file_path = os.path.join('uploads', filename)
            team_file.save(file_path)

            try:
                data = pd.read_csv(file_path) if filename.endswith('.csv') else pd.read_excel(file_path)
                required_columns = ['firstname', 'lastname', 'email', 'teamname']
                if any(col not in data.columns for col in required_columns):
                    flash(f"File must include columns: {', '.join(required_columns)}.", "danger")
                    return redirect(url_for('add_project', class_name=class_name))

                class_students = [student.id for student in db.collection('classrooms').document(class_name).collection('students').stream()]
                for _, row in data.iterrows():
                    student_email = row['email']
                    team_name = row['teamname']
                    student_name = f"{row['lastname']}, {row['firstname']}"

                    if student_email not in class_students:
                        flash(f"Student {student_name} is not part of this class.", "danger")
                        return redirect(url_for('add_project', class_name=class_name))

                    team_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name).collection('teams').document(team_name)
                    team_ref.set({student_email: student_name}, merge=True)

                teams_created = True
            except Exception as e:
                flash(f"Error processing team file: {str(e)}", "danger")
                return redirect(url_for('add_project', class_name=class_name))

        project_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name)
        project_ref.set({
            'projectName': project_name,
            'description': project_description,
            'dueDate': due_date,
            'createdAt': firestore.SERVER_TIMESTAMP,
        })

        if teams_created:
            flash(f"Project '{project_name}' with teams added to classroom {class_name}.", "success")
        else:
            flash(f"Project '{project_name}' added to classroom {class_name}. You can add teams later.", "success")
        return redirect(url_for('classroom_view', class_name=class_name))

    return render_template('add_project.html', class_name=class_name, current_date_time=current_date_time)

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
            flash("No data received.", "danger")
            return redirect(request.referrer)
        
        teams = data.get("teams", [])
        class_name = data.get("class_name")
        project_name = data.get("project_name")

        if not isinstance(teams, list):
            flash("Invalid format for teams. Expected a list.", "danger")
            return redirect(request.referrer)
        
        if not class_name or not project_name:
            flash("Class name or project name missing.", "danger")
            return redirect(request.referrer)

        # Reference to the Firestore collection for teams
        teams_collection_ref = db.collection('classrooms').document(class_name).collection('Projects').document(project_name).collection('teams')

        # Fetch existing teams from Firestore
        existing_teams = teams_collection_ref.stream()
        existing_team_data = {team.id: team.to_dict() for team in existing_teams}

        processed_students = set()

        for team in teams:
            team_name = team.get("teamName")
            students = team.get("students", [])

            if not team_name:
                flash("Team name is missing for one of the teams.", "danger")
                return redirect(request.referrer)
            
            if students is None:
                students = []

            team_data = {}
            for student_email in students:
                if student_email in processed_students:
                    continue

                processed_students.add(student_email)

                # Fetch student details from Firestore
                student_ref = db.collection('classrooms').document(class_name).collection('students').document(student_email).get()
                if student_ref.exists:
                    student_info = student_ref.to_dict()
                    # Save student details in the team data
                    team_data[student_email] = f"{student_info['lastName']}, {student_info['firstName']}"

                    # Remove the student from their old team if they are being moved
                    for old_team_name, old_team_data in existing_team_data.items():
                        if student_email in old_team_data:
                            del existing_team_data[old_team_name][student_email]

                            # Only update the team if it still has members
                            if existing_team_data[old_team_name]:
                                teams_collection_ref.document(old_team_name).set(existing_team_data[old_team_name])
                            else:
                                # Skip deletion of empty teams
                                flash(f"Team '{old_team_name}' is now empty, but will not be deleted.", "warning")
                else:
                    return jsonify({"error": f"Student {student_email} does not exist in the classroom."}), 400

            # Save the new team data
            teams_collection_ref.document(team_name).set(team_data)

        flash("Teams saved successfully!", "success")
        return redirect(request.referrer)

    except Exception as e:
        flash(f"An error occurred: {str(e)}", "danger")
        return redirect(request.referrer)

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