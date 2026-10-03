import os
import re
import shutil
import xml.etree.ElementTree as ET


def _open_tag_end(content, start):
    """start 指向 '<'，返回 (起始标签结束位置, 是否自闭合)。

    按引号感知的方式找 '>'，避免属性值里出现 '>' 时误判。
    """
    i = start + 1
    quote = None
    while i < len(content):
        ch = content[i]
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == '>':
            return i + 1, content[i - 1] == '/'
        i += 1
    raise ValueError('unterminated start tag at offset %d' % start)


# 资源的"类型"和它在 values XML 里的元素写法不是一回事，去重必须按类型：
#   <dimen name="x">1dp</dimen>            -> type="dimen"
#   <item type="dimen" name="x">1dp</item> -> type="dimen"（apktool 对部分格式用 item）
#   <array> / <integer-array> / <string-array> -> type="array"
# 否则 apktool 解出来的 <array name="cast_mode"> 会被当成和源码里的
# <integer-array name="cast_mode"> 不同的资源，追加出重复定义。
def _resource_type(tag, attrs_type):
    if tag == 'item':
        return attrs_type
    if tag in ('array', 'string-array', 'integer-array', 'typed-array'):
        return 'array'
    return tag


# apktool 反编译后按类型把资源拆进不同文件，追加新资源时要落到对应文件里。
# 源码里的 colors.xml / strings.xml 是混着放的（strings.xml 里就带着 array），
# 所以目标文件必须按资源类型选，不能跟着源码文件名走。
VALUES_FILE_FOR_TYPE = {
    'string': 'strings.xml',
    'array': 'arrays.xml',
    'plurals': 'plurals.xml',
    'dimen': 'dimens.xml',
    'bool': 'bools.xml',
    'integer': 'integers.xml',
    'color': 'colors.xml',
    'style': 'styles.xml',
    'attr': 'attrs.xml',
    'id': 'ids.xml',
    'drawable': 'drawables.xml',
    'fraction': 'fractions.xml',
}


def _values_file_for(res_type):
    return VALUES_FILE_FOR_TYPE.get(res_type, (res_type or 'misc') + 's.xml')


def _iter_children(content, parent_tag='resources'):
    """产出 (res_type, name, start, end)：<resources> 直接子元素的精确文本区间。

    按元素真实区间取原文，既不会误伤相邻元素，也不会破坏 android: / xliff:
    这类命名空间前缀（用 ElementTree 重新序列化是会把它们改写成 ns0: 的）。
    """
    parent = re.search(r'<' + re.escape(parent_tag) + r'(?=[\s/>])', content)
    if not parent:
        return
    open_end, self_closing = _open_tag_end(content, parent.start())
    if self_closing:
        return
    limit = content.find('</' + parent_tag + '>', open_end)
    if limit < 0:
        limit = len(content)

    child_open = re.compile(r'<([A-Za-z_][\w.:\-]*)(?=[\s/>])')
    pos = open_end
    while pos < limit:
        match = child_open.search(content, pos)
        if not match or match.start() >= limit:
            return
        tag = match.group(1)
        child_end, child_self_closing = _open_tag_end(content, match.start())
        if child_self_closing:
            end = child_end
        else:
            close = content.find('</' + tag + '>', child_end)
            if close < 0:
                return
            end = close + len(tag) + 3
        head = content[match.start():child_end]
        name_match = re.search(r'\bname\s*=\s*["\']([^"\']*)["\']', head)
        type_match = re.search(r'\btype\s*=\s*["\']([^"\']*)["\']', head)
        res_type = _resource_type(tag, type_match.group(1) if type_match else None)
        yield res_type, (name_match.group(1) if name_match else None), match.start(), end
        pos = end


def _root_ns_decls(content):
    """取出 <resources ...> 上声明的 xmlns:prefix -> uri。"""
    parent = re.search(r'<resources(?=[\s/>])', content)
    if not parent:
        return []
    open_end, _ = _open_tag_end(content, parent.start())
    head = content[parent.start():open_end]
    return re.findall(r'xmlns:([\w.\-]+)\s*=\s*"([^"]*)"', head)


def _ensure_ns_decls(content, decls):
    """把源文件根节点上的 xmlns:xxx 声明补到目标根节点上（缺失的才补）。

    源码里的字符串带 <xliff:g>，把元素原文搬过去时目标 <resources> 也得有
    对应的 xmlns 声明，否则 aapt2 报 "xml parser error: unbound prefix"。
    """
    if not decls:
        return content
    parent = re.search(r'<resources(?=[\s/>])', content)
    if not parent:
        return content
    open_end, _ = _open_tag_end(content, parent.start())
    head = content[parent.start():open_end]
    have = set(re.findall(r'xmlns:([\w.\-]+)\s*=', head))
    add = ''.join(f' xmlns:{prefix}="{uri}"' for prefix, uri in decls if prefix not in have)
    if not add:
        return content
    insert_at = open_end - 2 if content[open_end - 2] == '/' else open_end - 1
    return content[:insert_at] + add + content[insert_at:]


def merge_values_file(source_file, target_values_dir, replace_existing=True, tag_name=None):
    """把 source_file 里的资源项合并进 target_values_dir 这个 values 目录。

    同名（同类型）替换或保留，缺失的按资源类型追加到对应文件。

    绝不能用 `<tag ...>.*?</tag>` 这种正则做替换——目标文件里的同名元素
    可能是自闭合的 `<style name="X" ... />`，正则会从这个标签一路吞到
    下一个 `</tag>`，把中间的资源定义全部删掉（CI 上就是这么把 14 个
    Theme.AppCompat* 样式吃掉，导致 aapt2 报
    "no definition for declared symbol"）。这里按元素的真实文本区间替换。
    """
    if not os.path.exists(source_file):
        return

    with open(source_file, 'r', encoding='utf-8') as f:
        source_content = f.read()

    source_items = []
    for res_type, name, start, end in _iter_children(source_content):
        if not name or res_type in ('declare-styleable', 'eat-comment'):
            continue
        if tag_name is not None and res_type != tag_name:
            continue
        source_items.append((res_type, name, source_content[start:end]))
    if not source_items:
        return

    os.makedirs(target_values_dir, exist_ok=True)

    # 索引目标目录里所有已定义的资源：(res_type, name) -> (文件名, start, end)
    file_cache = {}
    index = {}
    for file_name in sorted(os.listdir(target_values_dir)):
        if not file_name.endswith('.xml'):
            continue
        path = os.path.join(target_values_dir, file_name)
        with open(path, 'r', encoding='utf-8') as f:
            file_content = f.read()
        file_cache[file_name] = file_content
        for res_type, name, start, end in _iter_children(file_content):
            if name and (res_type, name) not in index:
                index[(res_type, name)] = (file_name, start, end)

    edits = {}    # 文件名 -> [(start, end, text)]  就地替换
    appends = {}  # 文件名 -> [text]                追加新资源
    replaced = added = skipped = 0
    for res_type, name, text in source_items:
        key = (res_type, name)
        if key in index:
            if replace_existing:
                file_name, start, end = index[key]
                edits.setdefault(file_name, []).append((start, end, text))
                replaced += 1
            else:
                skipped += 1
        else:
            file_name = _values_file_for(res_type)
            appends.setdefault(file_name, []).append(text)
            index[key] = (file_name, None, None)  # 同一源文件内不重复追加
            added += 1

    ns_decls = _root_ns_decls(source_content)
    touched = set(edits) | set(appends)
    for file_name in sorted(touched):
        if file_name in file_cache:
            content = file_cache[file_name]
        else:
            content = '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n</resources>\n'

        # 从后往前改，前面的偏移量才不会失效
        for start, end, text in sorted(edits.get(file_name, []), key=lambda e: e[0], reverse=True):
            content = content[:start] + text + content[end:]

        if file_name in appends:
            pos = content.rfind('</resources>')
            if pos == -1:
                continue
            block = ''.join('    ' + text + '\n' for text in appends[file_name])
            content = content[:pos] + block + content[pos:]

        content = _ensure_ns_decls(content, ns_decls)
        path = os.path.join(target_values_dir, file_name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)

    label = tag_name or 'all'
    print(f"Merged {label} items from {source_file} into {target_values_dir} "
          f"(replaced {replaced}, added {added}, kept existing {skipped})")


VALUE_TYPES = {
    'style', 'string', 'dimen', 'bool', 'integer', 'array',
    'attr', 'fraction', 'plurals', 'id',
}


def verify_declared_symbols(apk_dir):
    """校验 public.xml 里声明的资源都有定义，提前暴露 aapt2 的
    "no definition for declared symbol"，错误信息直接点名缺哪些。"""
    res_dir = os.path.join(apk_dir, 'res')
    public_file = os.path.join(res_dir, 'values', 'public.xml')
    if not os.path.exists(public_file):
        return

    declared = set()
    for entry in ET.parse(public_file).getroot().findall('public'):
        res_type, res_name = entry.get('type'), entry.get('name')
        if res_type in VALUE_TYPES and res_name:
            declared.add((res_type, res_name))

    defined = set()
    for folder in os.listdir(res_dir):
        if not folder.startswith('values'):
            continue
        folder_path = os.path.join(res_dir, folder)
        if not os.path.isdir(folder_path):
            continue
        for file_name in os.listdir(folder_path):
            if not file_name.endswith('.xml'):
                continue
            file_path = os.path.join(folder_path, file_name)
            try:
                file_root = ET.parse(file_path).getroot()
            except ET.ParseError as exc:
                print(f'WARNING: cannot parse {file_path}: {exc}')
                continue
            for item in list(file_root):
                res_name = item.get('name')
                if res_name:
                    defined.add((_resource_type(item.tag, item.get('type')), res_name))

    missing = sorted(declared - defined)
    if missing:
        print(f'ERROR: public.xml declares {len(missing)} symbol(s) with no definition:')
        for res_type, res_name in missing:
            print(f'  - {res_type}/{res_name}')
        raise SystemExit(1)
    print(f'OK: all {len(declared)} declared symbols are defined.')


def apply_ui_patches(apk_dir, project_dir):
    # 0. Fix AndroidManifest.xml: remove android:pageSizeCompat (Android 15+ 16KB page attribute not in apktool framework)
    manifest_file = os.path.join(apk_dir, 'AndroidManifest.xml')
    if os.path.exists(manifest_file):
        with open(manifest_file, 'r', encoding='utf-8') as f:
            manifest_content = f.read()
        cleaned_manifest = re.sub(r'\s*android:pageSizeCompat="[^"]*"', '', manifest_content)
        with open(manifest_file, 'w', encoding='utf-8') as f:
            f.write(cleaned_manifest)
        print(f"Patched {manifest_file} (removed pageSizeCompat)")

    res_dir = os.path.join(apk_dir, 'res')
    if not os.path.exists(res_dir):
        raise FileNotFoundError(f"Res dir not found at {res_dir}")

    def source(*parts):
        return os.path.join(project_dir, 'app', 'src', *parts)

    def target(*parts):
        return os.path.join(res_dir, *parts)

    # 1. Colors / styles：整体替换，这正是"换 UI"要改的东西
    merge_values_file(source('main', 'res', 'values', 'colors.xml'),
                      target('values'), replace_existing=True)
    merge_values_file(source('main', 'res', 'values', 'styles.xml'),
                      target('values'), replace_existing=True)
    merge_values_file(source('mobile', 'res', 'values', 'styles.xml'),
                      target('values'), replace_existing=True)
    merge_values_file(source('mobile', 'res', 'values-v27', 'styles.xml'),
                      target('values-v27'), replace_existing=True)

    # 2. Strings / arrays：只补缺失，不改原包已有文案（新布局会引用到源码里
    #    新增的 string，比如 play_decode / player_audio_decode）
    for values_dir in ('values', 'values-zh-rCN', 'values-zh-rTW'):
        for source_dir in ('main', 'mobile'):
            for file_name in ('strings.xml', 'arrays.xml'):
                merge_values_file(source(source_dir, 'res', values_dir, file_name),
                                  target(values_dir), replace_existing=False)

    # 3. Copy drawables, colors, layouts
    copy_dirs = [
        (source('main', 'res', 'drawable'), target('drawable')),
        (source('mobile', 'res', 'drawable'), target('drawable')),
        (source('mobile', 'res', 'color'), target('color')),
        (source('mobile', 'res', 'layout'), target('layout')),
        (source('mobile', 'res', 'layout-sw600dp'), target('layout-sw600dp')),
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

    # 4. 先自查一遍，别把 "缺定义" 的问题留给 aapt2 报天书
    verify_declared_symbols(apk_dir)

    print(f"Successfully applied modern streaming cinema UI patches to {apk_dir}!")


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: python patch_apk.py <apk_decompiled_dir>")
        sys.exit(1)
    apply_ui_patches(sys.argv[1], os.path.dirname(os.path.abspath(__file__)))
