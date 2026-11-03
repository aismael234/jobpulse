"""The skills the wizard knows (M8): each with one case-insensitive pattern, used both to
find it on a resume and as a stack keyword in job descriptions."""

import re
from collections import Counter
from dataclasses import dataclass
from functools import cache


@dataclass(frozen=True, slots=True)
class Skill:
    key: str
    label: str
    group: str
    pattern: str


def _skill(group: str, label: str, pattern: str) -> Skill:
    spelled = label.lower().replace("+", "plus").replace("#", "sharp").replace(".net", "dotnet")
    key = re.sub(r"[^a-z0-9]+", "-", spelled).strip("-")
    return Skill(key, label, group, pattern)


_CATALOG: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Languages",
        (
            ("Python", r"\bpython\b"),
            ("Java", r"\bjava\b(?!\s*script)"),
            ("JavaScript", r"\bjavascript\b|\bes6\b"),
            ("TypeScript", r"\btypescript\b"),
            ("Go", r"\bgolang\b|\bgo (?:language|developer|engineer|services?)\b"),
            ("Rust", r"\brust\b"),
            ("C++", r"\bc\+\+|\bcpp\b"),
            ("C", r"(?<![\w+#])c(?![\w+#])(?=\s*(?:,|/|and|or|\)|programming))"),
            ("C#", r"\bc#|\bc sharp\b"),
            ("Ruby", r"\bruby\b"),
            ("PHP", r"\bphp\b"),
            ("Kotlin", r"\bkotlin\b"),
            ("Swift", r"\bswift\b(?!ui)"),
            ("Objective-C", r"\bobjective-c\b"),
            ("Scala", r"\bscala\b"),
            ("Elixir", r"\belixir\b"),
            ("Haskell", r"\bhaskell\b"),
            ("Clojure", r"\bclojure\b"),
            ("Dart", r"\bdart\b"),
            ("R", r"(?<![\w-])R(?![\w-])(?=\s*(?:,|/|and|programming|studio))"),
            ("MATLAB", r"\bmatlab\b"),
            ("Julia", r"\bjulia\b"),
            ("Perl", r"\bperl\b"),
            ("Lua", r"\blua\b"),
            ("Bash", r"\bbash\b|\bshell script"),
            ("SQL", r"\bsql\b"),
            ("Solidity", r"\bsolidity\b"),
            ("Zig", r"\bzig\b"),
            ("OCaml", r"\bocaml\b"),
            ("Erlang", r"\berlang\b"),
        ),
    ),
    (
        "Front end",
        (
            ("React", r"\breact(\.?js)?\b(?! native)"),
            ("Next.js", r"\bnext\.?js\b"),
            ("Vue", r"\bvue(\.?js)?\b"),
            ("Nuxt", r"\bnuxt\b"),
            ("Angular", r"\bangular(js)?\b"),
            ("Svelte", r"\bsvelte(kit)?\b"),
            ("Redux", r"\bredux\b"),
            ("HTML", r"\bhtml5?\b"),
            ("CSS", r"\bcss3?\b"),
            ("Tailwind", r"\btailwind\b"),
            ("Sass", r"\bsass\b|\bscss\b"),
            ("Webpack", r"\bwebpack\b"),
            ("Vite", r"\bvite\b"),
            ("GraphQL", r"\bgraphql\b"),
            ("jQuery", r"\bjquery\b"),
            ("Three.js", r"\bthree\.?js\b"),
            ("D3", r"\bd3(\.?js)?\b"),
        ),
    ),
    (
        "Back end",
        (
            ("Node.js", r"\bnode(\.?js)?\b"),
            ("Express", r"\bexpress\.?js\b|\bexpress\b(?= ?(?:,|/|and|framework))"),
            ("NestJS", r"\bnest\.?js\b"),
            ("Django", r"\bdjango\b"),
            ("Flask", r"\bflask\b"),
            ("FastAPI", r"\bfastapi\b"),
            ("Spring", r"\bspring( boot)?\b"),
            ("Rails", r"\brails\b"),
            ("Laravel", r"\blaravel\b"),
            (".NET", r"\.net\b|\basp\.net\b"),
            ("gRPC", r"\bgrpc\b"),
            ("REST APIs", r"\brest(ful)? apis?\b"),
            ("Microservices", r"\bmicroservices?\b"),
            ("Kafka", r"\bkafka\b"),
            ("RabbitMQ", r"\brabbitmq\b"),
            ("Celery", r"\bcelery\b"),
            ("WebSockets", r"\bwebsockets?\b"),
            ("Distributed systems", r"\bdistributed systems?\b"),
        ),
    ),
    (
        "Data stores",
        (
            ("PostgreSQL", r"\bpostgres(ql)?\b"),
            ("MySQL", r"\bmysql\b"),
            ("SQLite", r"\bsqlite\b"),
            ("MongoDB", r"\bmongo(db)?\b"),
            ("Redis", r"\bredis\b"),
            ("Elasticsearch", r"\belastic ?search\b|\bopensearch\b"),
            ("DynamoDB", r"\bdynamo ?db\b"),
            ("Cassandra", r"\bcassandra\b"),
            ("Snowflake", r"\bsnowflake\b"),
            ("BigQuery", r"\bbig ?query\b"),
            ("Redshift", r"\bredshift\b"),
            ("Supabase", r"\bsupabase\b"),
            ("Firebase", r"\bfirebase\b"),
            ("pgvector", r"\bpgvector\b"),
            ("Pinecone", r"\bpinecone\b"),
            ("Prisma", r"\bprisma\b"),
            ("SQLAlchemy", r"\bsqlalchemy\b"),
        ),
    ),
    (
        "Cloud and DevOps",
        (
            ("AWS", r"\baws\b|\bamazon web services\b"),
            ("Azure", r"\bazure\b"),
            ("GCP", r"\bgcp\b|\bgoogle cloud\b"),
            ("Docker", r"\bdocker\b"),
            ("Kubernetes", r"\bkubernetes\b|\bk8s\b"),
            ("Terraform", r"\bterraform\b"),
            ("Ansible", r"\bansible\b"),
            ("CI/CD", r"\bci ?/ ?cd\b"),
            ("GitHub Actions", r"\bgithub actions\b"),
            ("Jenkins", r"\bjenkins\b"),
            ("Linux", r"\blinux\b"),
            ("Serverless", r"\bserverless\b|\blambda\b"),
            ("Prometheus", r"\bprometheus\b"),
            ("Grafana", r"\bgrafana\b"),
            ("Datadog", r"\bdatadog\b"),
            ("Nginx", r"\bnginx\b"),
            ("Vercel", r"\bvercel\b"),
        ),
    ),
    (
        "Data",
        (
            ("Spark", r"\bspark\b|\bpyspark\b"),
            ("Airflow", r"\bairflow\b"),
            ("dbt", r"\bdbt\b"),
            ("Pandas", r"\bpandas\b"),
            ("NumPy", r"\bnumpy\b"),
            ("Hadoop", r"\bhadoop\b"),
            ("Flink", r"\bflink\b"),
            ("ETL", r"\betl\b|\belt pipelines?\b"),
            ("Tableau", r"\btableau\b"),
            ("Power BI", r"\bpower ?bi\b"),
            ("Looker", r"\blooker\b"),
            ("Databricks", r"\bdatabricks\b"),
        ),
    ),
    (
        "Machine learning",
        (
            ("PyTorch", r"\bpytorch\b"),
            ("TensorFlow", r"\btensorflow\b"),
            ("Keras", r"\bkeras\b"),
            ("JAX", r"\bjax\b"),
            ("scikit-learn", r"\bscikit[- ]learn\b|\bsklearn\b"),
            ("XGBoost", r"\bxgboost\b|\blightgbm\b"),
            ("Hugging Face", r"\bhugging ?face\b|\btransformers library\b"),
            ("LLMs", r"\bllms?\b|\blarge language models?\b"),
            ("RAG", r"\brag\b|\bretrieval[- ]augmented\b"),
            ("LangChain", r"\blang(chain|graph)\b"),
            ("LlamaIndex", r"\bllama ?index\b"),
            ("OpenAI API", r"\bopenai api\b|\bgpt-4"),
            ("Prompt engineering", r"\bprompt engineering\b"),
            ("Fine-tuning", r"\bfine[- ]tun(e|ing)\b|\blora\b"),
            ("Computer vision", r"\bcomputer vision\b"),
            ("OpenCV", r"\bopencv\b"),
            (
                "Object detection",
                r"\byolo\b|\bobject detection\b|\bimage classification\b|\bsegmentation\b",
            ),
            ("NLP", r"\bnlp\b|\bnatural language processing\b"),
            ("Reinforcement learning", r"\breinforcement learning\b"),
            ("MLOps", r"\bmlops\b|\bmlflow\b|\bkubeflow\b"),
            ("CUDA", r"\bcuda\b"),
            ("Recommender systems", r"\brecommend(er|ation) systems?\b"),
            ("Vector search", r"\bvector (search|databases?)\b|\bembeddings\b"),
            ("Deep learning", r"\bdeep learning\b|\bneural networks?\b"),
        ),
    ),
    (
        "Mobile",
        (
            ("iOS", r"\bios\b"),
            ("Android", r"\bandroid\b"),
            ("React Native", r"\breact native\b"),
            ("Flutter", r"\bflutter\b"),
            ("SwiftUI", r"\bswiftui\b"),
            ("Jetpack Compose", r"\bjetpack compose\b"),
            ("Expo", r"\bexpo\b"),
        ),
    ),
    (
        "Practices",
        (
            ("Git", r"\bgit\b"),
            ("Testing", r"\bunit test(s|ing)?\b|\bpytest\b|\bjest\b|\btdd\b"),
            ("Agile", r"\bagile\b|\bscrum\b"),
            ("System design", r"\bsystem design\b"),
            ("Security", r"\bapplication security\b|\bappsec\b|\boauth\b"),
            ("Embedded", r"\bembedded\b|\brtos\b"),
            ("Game development", r"\bunity\b|\bunreal engine\b"),
            ("Blockchain", r"\bblockchain\b|\bweb3\b|\bethereum\b"),
            ("Figma", r"\bfigma\b"),
        ),
    ),
)

SKILLS: tuple[Skill, ...] = tuple(
    _skill(group, label, pattern) for group, items in _CATALOG for label, pattern in items
)
SKILLS_BY_KEY: dict[str, Skill] = {skill.key: skill for skill in SKILLS}

# Weights a skill's answer gives its stack keyword.
MAIN_WEIGHT = 1.0
ALSO_WEIGHT = 0.5


@cache
def compiled(key: str) -> re.Pattern[str]:
    return re.compile(SKILLS_BY_KEY[key].pattern, re.IGNORECASE)


def find_skills(text: str) -> Counter[str]:
    """How many times each catalog skill appears in ``text``, for those that do."""
    counts: Counter[str] = Counter()
    for skill in SKILLS:
        hits = len(compiled(skill.key).findall(text))
        if hits:
            counts[skill.key] = hits
    return counts
