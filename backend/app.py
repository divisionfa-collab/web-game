import os

def show_tree(start_path, indent=""):
    """طباعة هيكل الملفات والمجلدات بشكل شجري."""
    try:
        entries = sorted(os.listdir(start_path))
    except PermissionError:
        print(indent + "⛔ [لا يمكن الوصول]")
        return

    for i, entry in enumerate(entries):
        full_path = os.path.join(start_path, entry)
        connector = "└── " if i == len(entries) - 1 else "├── "
        print(indent + connector + entry)
        if os.path.isdir(full_path):
            new_indent = indent + ("    " if i == len(entries) - 1 else "│   ")
            show_tree(full_path, new_indent)

if __name__ == "__main__":
    # ضع المسار الذي تريده هنا
    project_path = r"C:\Users\FAHAD\Downloads\game33\1"
    print(project_path)
    show_tree(project_path)
