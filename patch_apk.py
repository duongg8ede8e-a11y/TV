import os
import shutil
import xml.etree.ElementTree as ET
import re

def merge_xml_resources(source_file, target_file, tag_name='color'):
    """Merge resources (color, style, dimen, etc.) from source into target."""
    if not os.path.exists(source_file) or not os.path.exists(target_file):
        return
    
    with open(target_file, 'r', encoding='utf-8') as f:
        target_content = f.read()

    # Parse source
    tree = ET.parse(source_file)
    root = tree.getroot()

    for item in root.findall(tag_name):
        name = item.get('name')
        if not name:
            continue
        
        # Serialize item to string
        item_str = ET.tostring(item, encoding='utf-8').decode('utf-8').strip()

        # Check if already in target
        pattern = rf'<{tag_name}\s+name=["\']{re.escape(name)}["\'][^>]*>.*?</{tag_name}>|<{tag_name}\s+name=["\']{re.escape(name)}["\'][^/>]*/>'
        match = re.search(pattern, target_content, flags=re.DOTALL)
        if match:
            # Replace existing
            target_content = target_content[:match.start()] + item_str + target_content[match.end():]
        else:
            # Insert before </resources>
            pos = target_content.rfind('</resources>')
            if pos != -1:
                target_content = target_content[:pos] + '    ' + item_str + '\n' + target_content[pos:]

    with open(target_file, 'w', encoding='utf-8') as f:
        f.write(target_content)
    print(f"Merged {tag_name} items from {source_file} into {target_file}")

def apply_ui_patches(apk_dir, project_dir):
    res_dir = os.path.join(apk_dir, 'res')
    if not os.path.exists(res_dir):
        raise FileNotFoundError(f"Res dir not found at {res_dir}")

    # 1. Merge colors
    source_colors = os.path.join(project_dir, 'app', 'src', 'main', 'res', 'values', 'colors.xml')
    target_colors = os.path.join(res_dir, 'values', 'colors.xml')
    merge_xml_resources(source_colors, target_colors, 'color')

    # 2. Merge styles
    source_styles_main = os.path.join(project_dir, 'app', 'src', 'main', 'res', 'values', 'styles.xml')
    target_styles = os.path.join(res_dir, 'values', 'styles.xml')
    merge_xml_resources(source_styles_main, target_styles, 'style')

    source_styles_mobile = os.path.join(project_dir, 'app', 'src', 'mobile', 'res', 'values', 'styles.xml')
    merge_xml_resources(source_styles_mobile, target_styles, 'style')

    # 3. Copy drawables, colors, layouts
    copy_dirs = [
        (os.path.join(project_dir, 'app', 'src', 'main', 'res', 'drawable'), os.path.join(res_dir, 'drawable')),
        (os.path.join(project_dir, 'app', 'src', 'mobile', 'res', 'drawable'), os.path.join(res_dir, 'drawable')),
        (os.path.join(project_dir, 'app', 'src', 'mobile', 'res', 'color'), os.path.join(res_dir, 'color')),
        (os.path.join(project_dir, 'app', 'src', 'mobile', 'res', 'layout'), os.path.join(res_dir, 'layout')),
        (os.path.join(project_dir, 'app', 'src', 'mobile', 'res', 'layout-sw600dp'), os.path.join(res_dir, 'layout-sw600dp')),
    ]

    for src, dst in copy_dirs:
        if os.path.exists(src):
            os.makedirs(dst, exist_ok=True)
            for item in os.listdir(src):
                s_path = os.path.join(src, item)
                d_path = os.path.join(dst, item)
                if os.path.isfile(s_path):
                    shutil.copy2(s_path, d_path)
                    print(f"Copied: {item} -> {os.path.relpath(d_path, apk_dir)}")

    print(f"Successfully applied modern streaming cinema UI patches to {apk_dir}!")

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: python patch_apk.py <apk_decompiled_dir>")
        sys.exit(1)
    apply_ui_patches(sys.argv[1], os.path.dirname(os.path.abspath(__file__)))
