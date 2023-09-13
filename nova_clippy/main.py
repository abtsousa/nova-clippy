#Import
from datetime import datetime
from pathlib import Path
import logging as log
import concurrent.futures as cf
import typer
import time
from typing_extensions import Annotated
from typing import Optional
from InquirerPy import inquirer
from rich import print

#Config
import nova_clippy.config as cfg

# Local modules
from modules.LoginError import LoginError
from modules.CourseList import CourseList
from modules.Course import Course

# Local functions
from handlers.get_login import get_login
from handlers.HTML_parser import parse_courses, parse_docs, parse_index, parse_years
from handlers.file_handler import get_file, download_file, count_files_in_subfolders
from handlers.cache_handler import commit_cache, parse_cache, stash_cache
from handlers.print_handler import print_progress, human_readable_size

"""
NOVA Clippy
A simple web scraper and downloader for FCT-NOVA's internal e-learning platform, CLIP.
The program scrapes a user's courses for available downloads and syncs them with a local folder.

CLIP's files are organized in subcategories for each academic course like this:
Academic year >> Course documents >> Document subcategory >> Files list

Clippy successfully navigates the site in order to scrape it, and compares it to a local folder
with a similar structure, keeping it in sync with the server.
"""

"""
 __                 
/  \        _______________________ 
|  |       /                       \
@  @       | It looks like you     |
|| ||      | are downloading files |
|| ||   <--| from CLIP. Do you     |
|\_/|      | need assistance?      |
\___/      \_______________________/
"""

# TODO create package https://typer.tiangolo.com/tutorial/package/ and https://stackoverflow.com/questions/20101834/pip-install-from-git-repo-branch
# TODO Distribute as exe?
# TODO remove pandas and any other unneccessary dependencies
# TODO generate dependencies?

# The code mimics the site's structure, as follows:
#       CLIP: Academic year   >> Course >> Document subcategory >> Files list >>   File
#     Clippy:  CourseList     >> Course >>      CatCount        >>  FilesList >> ClipFile
# Local copy:      Year       /  Course /       Category        /     Files

__author__ = "Afonso Bras Sousa (LEI-65263)"
__maintainer__ = "Afonso Bras Sousa"
__email__ = "ab.sousa@campus.fct.unl.pt"
__version__ = "0.9b2"

app = typer.Typer()

@app.command()
def main(username: Annotated[str, typer.Option(help="O nome de utilizador no CLIP.", show_default=False)] = None,
        path: Annotated[Optional[Path], typer.Argument(help="A pasta onde os ficheiros do CLIP serão guardados. (opcional)", show_default=False)] = None,
        force_relogin: Annotated[bool, typer.Option(help="Ignora as credenciais guardadas em sistema.")] = False,
        auto: Annotated[bool, typer.Option(help="Escolhe automaticamente o ano lectivo mais recente.")] = True,
        debug: Annotated[bool, typer.Option(help="Cria um ficheiro log.log para efeitos de debug.", hidden = True)] = False,
    ):
    """\bO Clippy é um simples web scrapper e gestor de downloads para a plataforma interna de e-learning da FCT-NOVA, o CLIP.
    O programa navega o CLIP à procura de ficheiros nas páginas das cadeiras de um utilizador e sincroniza-os com uma pasta local.
     __                 
    /  \\        _______________________ 
    |  |       /                       \\
    @  @       | Parece que estás a    |
    || ||      | tentar descarregar    |
    || ||   <--| ficheiros do CLIP.    |
    |\\_/|      | Precisas de ajuda?    |
    \\___/      \\_______________________/
    """

    # Logging
    logger = log.getLogger()
    logger.handlers.clear() #clear default logger
    logger.setLevel(log.DEBUG if debug else log.WARNING)

    #Console log
    console_formatter = log.Formatter('[%(levelname)s] %(message)s')
    console_logging = log.StreamHandler()
    console_logging.setLevel(log.DEBUG if debug else log.WARNING)
    console_logging.setFormatter(console_formatter)
    logger.addHandler(console_logging)

    #File log
    if debug:
        formatter = log.Formatter(
            '%(asctime)s.%(msecs)03d [%(levelname)s] %(module)s - %(funcName)s [%(lineno)s]: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        )
        file_logging = log.FileHandler('debug.log')
        file_logging.setLevel(log.DEBUG)
        file_logging.setFormatter(formatter)
        logger.addHandler(file_logging)

    if path is None: print(f"A iniciar o Clippy na directoria {Path.cwd()}...")
    # Check valid path
    path = check_path(path)

    # 0/5 Start login
    valid_login = False
    while not valid_login:
        try:
            if username is None and not force_relogin:
                user = get_login(cfg.username, cfg.password)
            else:
                user = get_login(username)
            valid_login = True
        except LoginError:
            continue
    
    years = parse_years(user)
    if len(years)<1:
        log.error("Não foram encontrados anos lectivos nos quais o utilizador está inscrito.")
        exit()
    elif len(years)==1:
        year = list(years.values())[0] # get index 0
        log.info(f"Encontrado apenas um ano lectivo ({year}).")
    elif auto:
        year = sorted(list(years.values()))[-1] # get index 0
        log.info("Modo automático activo, a escolher o ano lectivo mais recente...")
    else:
        year = inquirer.rawlist( #TODO multiselect
            message="Qual é o ano lectivo a transferir?",
            choices=[
                {"name": key, "value": value} for key, value in years.items()
            ],
            default=1,
            max_height=len(years)
        ).execute()
    log.debug(year)

    # 1) Scrape units list
    print_progress(1,"A procurar unidades curriculares inscritas...")
    courses = parse_courses(year,user)
    log.info("Encontradas as seguintes unidades: "+" | ".join(course.name for course in courses) )

    # 2) (Multithreaded) Load each unit's index and compare it to cached file if it exists
    print_progress(2, "A verificar se há ficheiros novos...")
    subcats = threadpool_execute(search_cats_in_course, [(path, course) for course in courses])
    log.debug(f"Lista de subcategorias a procurar: {subcats}")

    # 3) (Multithreaded) Load each subcategory's table and compare it to the local folder
    print_progress(3, "A obter URLs dos ficheiros a transferir...")
    files = threadpool_execute(search_files_in_category, subcats)
    log.debug(f"Lista de ficheiros a transferir: {files}")
    
    # 4) (Multithreaded) Download missing files
    if len(files) != 0:
        download_timestart = time.time_ns()
        download_sizestart = sum(f.stat().st_size for f in path.glob('**/*') if f.is_file())
        print_progress(4, "A transferir ficheiros em falta...")
        _ = threadpool_execute(download_file, files, max_workers=4)
        print_progress(4,"Todos os ficheiros foram transferidos.")
    else:
        print_progress(4, "Não há ficheiros a transferir.")

    # 5) Update cache after successful download
    print_progress(5, "A actualizar cache...")
    commit_cache()

    # 6) Exit with success
    print_progress(6, "Concluído :)")
    if len(files) != 0:
        download_time = (time.time_ns() - download_timestart) / 10**9
        download_size = (sum(f.stat().st_size for f in path.glob('**/*') if f.is_file())) - download_sizestart
        unique_folders = sorted({str(file[0].parent) for file in files})
        print(f"Transferidos {len(files)} ficheiros ({human_readable_size(download_size)} em [dim cyan bold]{download_time}[/dim cyan bold]s) para as pastas:",flush=True)
        print("\n".join(f"'{folder}'" for folder in unique_folders))
    else:
        print("Não foram encontrados ficheiros novos.")
    
    cfg.save_config()


def threadpool_execute(worker_function, items, max_workers=cfg.MAX_THREADS):
    results = []
    with cf.ThreadPoolExecutor(max_workers=max_workers) as pool: 
        futures = {pool.submit(worker_function, *args): args for args in items}
        
        for future in cf.as_completed(futures):
            args = futures[future]
            try:
                result = future.result()  # Get the result from the future
                if result is not None:
                    results.extend(result)
            except Exception as e:
                log.error(f"Erro a processar {args}: {e}")
    
    return results

def check_path(path: Path):
    if path is None:
        path = Path.cwd()
        if path.name != "CLIP": path = path / "CLIP"
    if not path.exists():
        if inquirer.confirm(
            message=f"A directoria {path} não existe. Criá-la?",
            default=True,
            confirm_letter="s",
            reject_letter="n",
            transformer=lambda result: "Sim" if result else "Não",
        ).execute():
            path.mkdir(parents=True, exist_ok=True)
            print(f"A criar a directoria {path}.")
        else:
            path = query_path(path)
            check_path(path)
        #TODO check for config file in directory?
        #TODO default directory input instead of cwd?
    elif not path.is_dir():
        print("O caminho desejado não é uma directoria válida.")
        path = query_path(path)
        check_path(path)
    return path

def query_path(path: Path = None):
    return Path(inquirer.filepath(
                message="Introduza a directoria onde pretende guardar os ficheiros:",
                default = str(path),
                only_directories=True,
            ).execute()).expanduser()

def dict_compare(dict_a: dict, dict_b: dict):
    if dict_b is None: return dict_a
    elif dict_a is None: return dict_b
    else: return {key: dict_a[key] for key in dict_a.keys() if key not in dict_b or dict_a[key] > dict_b[key]}

def search_cats_in_course(path: Path, course: Course) -> [(str, str, Course, Path)]:
    print(f"A procurar documentos de {course.name}...")
    path = path / course.year

    index = parse_index(course.year, course.semester_type, course.semester, course.ID)

    if not index: #skips creating directory if there are no documents
        log.info(f"Não foram encontrados documentos em {course.name}.")
    else:
        log.debug(f"Contagem para {course.name}: {index}")
        full_semester = course.semester+course.semester_type.upper()
        full_path = path / full_semester / course.name

        full_path.mkdir(parents=True, exist_ok=True) # Create folder if it does not exist

        # Cache management
        cachedict = parse_cache(full_path, index, course.name)
        cachediff = dict_compare(index, cachedict)

        _subcats = []
        
        if not cachediff:
            log.debug(f"Sem diferenças para {course.name} em relação à contagem em cache.")
        else:
            log.debug(f"Em cache: {cachedict}")
            log.debug(f"No servidor: {index}")
            log.info(f"Categorias de {course.name} com novos ficheiros no servidor desde a última actualização: {cachediff}")
            # Update cache only if there are differences
            stash_cache(index,full_path)

            for category,count in cachediff.items():
                _subcats.append((category,index.get_catID(category),course,full_path))
                #search_files_in_category(category,index.get_catID(category),course,full_path)
        
        folderdict = count_files_in_subfolders(full_path)
        folderdiff = dict_compare(cachedict, folderdict)

        if not folderdiff:
            log.debug(f"Sem diferenças para {course.name} em relação à contagem de ficheiros.")
        else:
            log.warning(f"A contagem de ficheiros em {course.name} não coincide com a da última actualização. Quaisquer ficheiros apagados serão transferidos novamente.")
            log.debug(f"Na pasta: {folderdict}")
            log.debug(f"Em cache: {cachedict}")
            log.info(f"Pastas de {course.name} com menos ficheiros que a contagem em cache: {folderdiff}")

            for category,count in folderdiff.items():
                _subcats.append((category,index.get_catID(category),course,full_path))
                #search_files_in_category(category,index.get_catID(category),course,full_path)

        log.debug(f"Subcategorias de {course.name}: {_subcats}")
        return _subcats

def search_files_in_category(category: str, catID: str, course: Course, full_path: Path) -> [(Path, str, int, datetime)]:
    """
    Search for files in a specific category and download them if needed.

    Parameters:
        category (str): The category of files to search for.
        catID (str): The ID of respective category.
        course (Course): The course for which to search documents.
        full_path (Path): The full path to the directory where files should be downloaded.
    """
    try:
        print(f"A procurar {category} de {course.name}...")
        table = parse_docs(course.year,course.semester_type, course.semester, course.ID, catID)

        _files = []
        for file in table:
            folder = full_path / category
            log.debug(f"A procurar {file} na pasta {folder}...")
            _file = get_file(file,folder)
            if _file is not None:
                _files.append(_file)
        
        log.debug(f"Ficheiros de {course} > {category}: {_files}")
        return _files

    except Exception as ex:
        log.error(f'Erro a procurar {category} de {course}: {str(ex)}')
        pass

if __name__ == "__main__":
    app()