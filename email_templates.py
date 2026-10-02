"""The email styles a user can pick, each written in English and in French.

A template owns the connective wording — how the email opens, how the match
is introduced, how it closes. The user's profile owns every sentence about
them (who they are, what they built, what they're asking for), taken from
their CV and approved by them. build_spec() renders the two together into
the same spec shape as the original hand-written specializations.json, so
the composer, the research agent and the draft guard treat a template user
exactly like the account that was tuned by hand.

Two kinds of placeholders:
  <<identity>>   filled from the profile when the spec is built;
  {company}      filled per company by the composer ({hook}, {area_1},
                 {topic}, {applicant_name}, {target_role}, {company_de}).

The French wording is deliberately free of grammatical gender in the
template's own words ("Ce serait un plaisir", never "je serais ravi(e)"), so
it reads naturally for anyone; the profile's own French sentences carry
whatever form the user chose.
"""

from __future__ import annotations

import copy
import re

TEMPLATES = {
    # ------------------------------------------------------------------
    "specialist": {
        "name": {"en": "Specialist match", "fr": "Correspondance experte"},
        "description": {
            "en": "Opens on what the company does (quoted from its own site), matches it to your "
                  "strongest experience, then your other highlights. The proven default.",
            "fr": "S'ouvre sur l'activité de l'entreprise (citée depuis son site), la relie à votre "
                  "expérience la plus solide, puis vos autres atouts. Le modèle éprouvé par défaut.",
        },
        "en": {
            "subject": "{target_role} from <<start_date>> — {topic} | {applicant_name}",
            "intro_with_hook": "I'm <<identity>>. What interests me most about {company} is your work on {hook}.",
            "intro_standard_variants": [
                "I'm <<identity>>, and I'm really interested in becoming part of the team at {company}.",
                "I'm <<identity>>, and I'd genuinely like to join the team at {company}.",
                "I'm <<identity>>, and joining the {company} team is something I'm really interested in.",
            ],
            "match_lead_one": "Your focus on {area_1} is where my own experience is strongest.",
            "match_lead_two": "Your focus on {area_1} and {area_2} maps directly onto my own experience.",
            "closing_variants": [
                "My CV is attached with more detail. I'd be glad to discuss how I could contribute to {company}, and I look forward to hearing from you.",
                "My CV is attached with further detail. I'd be genuinely glad to bring this background to {company} and grow with the team — I look forward to hearing from you.",
            ],
            "sign_off": "Best regards,\n{applicant_name}",
        },
        "fr": {
            "subject": "{target_role} à partir de <<start_date>> — {topic} | {applicant_name}",
            "intro_with_hook": "Je suis <<identity>>. Ce qui m'intéresse le plus chez {company}, c'est votre travail sur {hook}.",
            "intro_standard_variants": [
                "Je suis <<identity>>, et rejoindre l'équipe {company_de} m'intéresse vraiment.",
                "Je suis <<identity>>, et j'aimerais sincèrement intégrer l'équipe {company_de}.",
                "Je suis <<identity>>, et faire partie de l'équipe {company_de} est une perspective qui me motive réellement.",
            ],
            "match_lead_one": "Votre expertise en {area_1} correspond précisément au domaine où mon expérience est la plus solide.",
            "match_lead_two": "Votre expertise en {area_1} et en {area_2} rejoint directement mon propre parcours.",
            "closing_variants": [
                "Vous trouverez mon CV en pièce jointe pour plus de détails. Ce serait un plaisir d'échanger sur la manière dont je pourrais contribuer à {company} ; dans l'attente de votre retour, je vous remercie de votre attention.",
                "Mon CV, joint à ce message, détaille davantage mon parcours. J'aimerais sincèrement mettre ce bagage au service de {company} et progresser avec l'équipe — au plaisir d'échanger avec vous.",
            ],
            "sign_off": "Cordialement,\n{applicant_name}",
        },
    },
    # ------------------------------------------------------------------
    "concise": {
        "name": {"en": "Short & direct", "fr": "Court et direct"},
        "description": {
            "en": "Four short paragraphs for busy recruiters: why them, your best match, one "
                  "highlight, the ask.",
            "fr": "Quatre paragraphes courts pour des recruteurs pressés : pourquoi eux, votre "
                  "meilleure correspondance, un atout, la demande.",
        },
        "layout": ["intro", "match", "strengths", "ask", "closing"],
        "strengths_budget": [1, 2],
        "include_motivation": False,
        "min_words": 70,
        "en": {
            "subject": "{target_role} ({topic}) — {applicant_name}",
            "intro_with_hook": "I'm <<identity>>, and your work on {hook} is exactly why I'm writing to {company}.",
            "intro_standard_variants": [
                "I'm <<identity>>, and I'd like to join the team at {company}.",
                "I'm <<identity>>, and I'm writing because I'd like to join {company}.",
            ],
            "match_lead_one": "This lines up with my experience in {area_1}.",
            "match_lead_two": "This lines up with my experience in {area_1} and {area_2}.",
            "closing_variants": [
                "My CV is attached — I'd welcome a short call whenever it suits you.",
                "My CV is attached, and I'd be happy to talk whenever convenient.",
            ],
            "sign_off": "Best regards,\n{applicant_name}",
        },
        "fr": {
            "subject": "{target_role} ({topic}) — {applicant_name}",
            "intro_with_hook": "Je suis <<identity>>, et votre travail sur {hook} est précisément la raison de mon message à {company}.",
            "intro_standard_variants": [
                "Je suis <<identity>>, et j'aimerais rejoindre l'équipe {company_de}.",
                "Je suis <<identity>>, et je vous écris car j'aimerais rejoindre {company}.",
            ],
            "match_lead_one": "Cela rejoint directement mon expérience en {area_1}.",
            "match_lead_two": "Cela rejoint directement mon expérience en {area_1} et en {area_2}.",
            "closing_variants": [
                "Mon CV est en pièce jointe ; un court échange serait un plaisir, quand cela vous convient.",
                "Vous trouverez mon CV en pièce jointe — au plaisir d'en discuter avec vous.",
            ],
            "sign_off": "Cordialement,\n{applicant_name}",
        },
    },
    # ------------------------------------------------------------------
    "project_led": {
        "name": {"en": "Project first", "fr": "Projet en premier"},
        "description": {
            "en": "Leads with your flagship project in the first line, then connects it to the "
                  "company. Strong when you have one standout achievement.",
            "fr": "Commence par votre projet phare dès la première ligne, puis le relie à "
                  "l'entreprise. Idéal quand une réalisation se démarque.",
        },
        "layout": ["flagship", "intro", "match", "strengths", "ask", "closing"],
        "strengths_budget": [1, 2],
        "en": {
            "subject": "{target_role} from <<start_date>> — {topic} | {applicant_name}",
            "intro_with_hook": "I'm <<identity>>, and what draws me to {company} is your work on {hook}.",
            "intro_standard_variants": [
                "I'm <<identity>>, and I'd like to bring that same drive to the team at {company}.",
                "I'm <<identity>>, and I'd genuinely like to put that experience to work at {company}.",
            ],
            "match_lead_one": "Your focus on {area_1} is where I can contribute most directly.",
            "match_lead_two": "Your focus on {area_1} and {area_2} is where I can contribute most directly.",
            "closing_variants": [
                "My CV is attached with the details. I'd be glad to talk about how I could contribute to {company}.",
                "My CV is attached — I look forward to hearing from you.",
            ],
            "sign_off": "Best regards,\n{applicant_name}",
        },
        "fr": {
            "subject": "{target_role} à partir de <<start_date>> — {topic} | {applicant_name}",
            "intro_with_hook": "Je suis <<identity>>, et ce qui m'attire chez {company}, c'est votre travail sur {hook}.",
            "intro_standard_variants": [
                "Je suis <<identity>>, et j'aimerais mettre cette même énergie au service de l'équipe {company_de}.",
                "Je suis <<identity>>, et j'aimerais sincèrement mettre cette expérience au service de {company}.",
            ],
            "match_lead_one": "Votre expertise en {area_1} est le domaine où je peux contribuer le plus directement.",
            "match_lead_two": "Votre expertise en {area_1} et en {area_2} est le domaine où je peux contribuer le plus directement.",
            "closing_variants": [
                "Mon CV, en pièce jointe, donne tous les détails. Ce serait un plaisir d'échanger sur ma possible contribution chez {company}.",
                "Mon CV est en pièce jointe — au plaisir d'échanger avec vous.",
            ],
            "sign_off": "Cordialement,\n{applicant_name}",
        },
    },
    # ------------------------------------------------------------------
    "formal": {
        "name": {"en": "Formal", "fr": "Formel"},
        "description": {
            "en": "A classic, formal application — for large companies, public institutions and "
                  "traditional sectors.",
            "fr": "Une candidature classique et formelle — pour les grands groupes, les "
                  "institutions publiques et les secteurs traditionnels.",
        },
        "en": {
            "subject": "Application — {target_role} from <<start_date>> | {applicant_name}",
            "intro_with_hook": "I am <<identity>>, and I would like to apply for an internship at {company}, whose work on {hook} closely matches my interests.",
            "intro_standard_variants": [
                "I am <<identity>>, and I would like to apply for an internship at {company}.",
                "I am <<identity>>, and I am applying for an internship within {company}.",
            ],
            "match_lead_one": "Your activity in {area_1} corresponds closely to my own experience.",
            "match_lead_two": "Your activities in {area_1} and {area_2} correspond closely to my own experience.",
            "closing_variants": [
                "Please find my CV attached. I would welcome the opportunity to discuss how I could contribute to {company}. Thank you for your time and consideration.",
                "My CV is attached for your consideration. I remain at your disposal for an interview, and thank you for your attention.",
            ],
            "sign_off": "Kind regards,\n{applicant_name}",
        },
        "fr": {
            "subject": "Candidature — {target_role} à partir de <<start_date>> | {applicant_name}",
            "intro_with_hook": "Actuellement <<identity>>, je me permets de vous adresser ma candidature pour un stage au sein de {company}, dont le travail sur {hook} rejoint pleinement mes centres d'intérêt.",
            "intro_standard_variants": [
                "Actuellement <<identity>>, je me permets de vous adresser ma candidature pour un stage au sein de {company}.",
                "Actuellement <<identity>>, je vous adresse ma candidature pour un stage au sein de {company}.",
            ],
            "match_lead_one": "Votre activité en {area_1} correspond étroitement à mon expérience.",
            "match_lead_two": "Vos activités en {area_1} et en {area_2} correspondent étroitement à mon expérience.",
            "closing_variants": [
                "Vous trouverez ci-joint mon CV. Je me tiens à votre disposition pour un entretien et vous prie d'agréer l'expression de mes salutations distinguées.",
                "Mon CV est joint à ce message. Restant à votre disposition pour un entretien, je vous prie d'agréer mes sincères salutations.",
            ],
            "sign_off": "{applicant_name}",
        },
    },
    # ------------------------------------------------------------------
    "research_lab": {
        "name": {"en": "Research & R&D", "fr": "Recherche & R&D"},
        "description": {
            "en": "For laboratories, R&D teams and research internships: frames your work as "
                  "research and asks to contribute to theirs.",
            "fr": "Pour les laboratoires, équipes R&D et stages de recherche : présente votre "
                  "travail comme de la recherche et propose de contribuer au leur.",
        },
        "en": {
            "subject": "Research {target_role} from <<start_date>> — {topic} | {applicant_name}",
            "intro_with_hook": "I'm <<identity>>. I'm writing because the research at {company} on {hook} is closely related to what I want to work on.",
            "intro_standard_variants": [
                "I'm <<identity>>, and I'd like to contribute to the research carried out at {company}.",
                "I'm <<identity>>, and I'm very interested in joining the research team at {company}.",
            ],
            "match_lead_one": "Your work in {area_1} is close to what I have done so far.",
            "match_lead_two": "Your work in {area_1} and {area_2} is close to what I have done so far.",
            "closing_variants": [
                "My CV is attached. I would be glad to discuss a possible research internship with your team.",
                "My CV is attached with more detail — I would be glad to discuss how I could contribute to your research.",
            ],
            "sign_off": "Best regards,\n{applicant_name}",
        },
        "fr": {
            "subject": "{target_role} en recherche à partir de <<start_date>> — {topic} | {applicant_name}",
            "intro_with_hook": "Je suis <<identity>>. Je vous écris car les recherches menées chez {company} sur {hook} sont très proches de ce sur quoi je souhaite travailler.",
            "intro_standard_variants": [
                "Je suis <<identity>>, et j'aimerais contribuer aux recherches menées chez {company}.",
                "Je suis <<identity>>, et rejoindre l'équipe de recherche {company_de} m'intéresse vivement.",
            ],
            "match_lead_one": "Vos travaux en {area_1} sont proches de ce que j'ai réalisé jusqu'ici.",
            "match_lead_two": "Vos travaux en {area_1} et en {area_2} sont proches de ce que j'ai réalisé jusqu'ici.",
            "closing_variants": [
                "Vous trouverez mon CV en pièce jointe. Ce serait un plaisir d'échanger sur un éventuel stage de recherche au sein de votre équipe.",
                "Mon CV, joint à ce message, détaille mon parcours — ce serait un plaisir d'échanger sur ma possible contribution à vos travaux.",
            ],
            "sign_off": "Cordialement,\n{applicant_name}",
        },
    },
    # ------------------------------------------------------------------
    # Jason Chen's method (CRV): show you know their work, link it to yours,
    # and ask for a short call rather than a job. A small ask is easy to say
    # yes to, and a question at the end draws replies.
    "conversation": {
        "name": {"en": "Short call request", "fr": "Demande d'échange"},
        "description": {
            "en": "Asks for a 15-minute call instead of a job: what you know of their work, how it "
                  "links to yours, then one easy question. Under 125 words.",
            "fr": "Demande un échange de 15 minutes plutôt qu'un poste : ce que vous savez de leur "
                  "travail, le lien avec le vôtre, puis une question simple. Moins de 125 mots.",
        },
        "layout": ["intro", "match", "ask", "closing"],
        "strengths_budget": [0, 1],
        "include_motivation": False,
        "min_words": 55,
        "max_words": 170,
        "en": {
            "subject": "Quick call about {topic}?",
            "intro_with_hook": "I'm <<identity>>. I've been reading about {company}'s work on {hook}, and I'd like to learn more about it.",
            "intro_standard_variants": [
                "I'm <<identity>>, and I'd like to learn more about the work your team does at {company}.",
                "I'm <<identity>>, and {company} is a team I'd really like to learn from.",
            ],
            "match_lead_one": "It connects with my own work in {area_1}.",
            "match_lead_two": "It connects with my own work in {area_1} and {area_2}.",
            "ask_text": "Would you have 15 minutes in the coming days for a short call? I'd like to hear how your team works, and whether there could be room for an intern from <<start_date>>.",
            "closing_variants": [
                "My CV is attached in case it's useful.",
                "I've attached my CV for context — I hope we can talk.",
            ],
            "sign_off": "Thanks in advance,\n{applicant_name}",
        },
        "fr": {
            "subject": "Un court échange sur {topic} ?",
            "intro_with_hook": "Je suis <<identity>>. Je m'intéresse au travail de {company} sur {hook}, et j'aimerais en apprendre davantage.",
            "intro_standard_variants": [
                "Je suis <<identity>>, et j'aimerais en savoir plus sur le travail de votre équipe chez {company}.",
                "Je suis <<identity>>, et {company} est une équipe auprès de laquelle j'aimerais beaucoup apprendre.",
            ],
            "match_lead_one": "Cela rejoint directement mon propre travail en {area_1}.",
            "match_lead_two": "Cela rejoint directement mon propre travail en {area_1} et en {area_2}.",
            "ask_text": "Auriez-vous 15 minutes dans les prochains jours pour un court échange ? J'aimerais comprendre comment travaille votre équipe, et savoir s'il pourrait y avoir une place pour un stage à partir de <<start_date>>.",
            "closing_variants": [
                "Mon CV est en pièce jointe, au cas où il vous serait utile.",
                "Vous trouverez mon CV en pièce jointe pour le contexte — au plaisir d'échanger.",
            ],
            "sign_off": "Merci par avance,\n{applicant_name}",
        },
    },
    # ------------------------------------------------------------------
    # The spontaneous application: what you ask for — the internship, its
    # dates, its length — comes second, before anything else, so the reader
    # sees at once whether it fits their plans.
    "spontaneous": {
        "name": {"en": "Spontaneous application", "fr": "Candidature spontanée"},
        "description": {
            "en": "Says right after the first line what you ask for — the internship, its dates and "
                  "length — then why them and your best match, and ends on a question.",
            "fr": "Dit dès la deuxième phrase ce que vous demandez — le stage, ses dates et sa "
                  "durée — puis pourquoi eux et votre meilleur atout, et finit par une question.",
        },
        "layout": ["intro", "ask", "match", "strengths", "closing"],
        "strengths_budget": [1, 1],
        "include_motivation": False,
        "min_words": 60,
        "max_words": 200,
        "en": {
            "subject": "Internship from <<start_date>> — {topic}",
            "intro_with_hook": "I'm <<identity>>, and I'm writing to {company} with a spontaneous application — your work on {hook} is why I chose you.",
            "intro_standard_variants": [
                "I'm <<identity>>, and I'm writing to {company} with a spontaneous internship application.",
                "I'm <<identity>>, and I'd like to send {company} a spontaneous internship application.",
            ],
            "match_lead_one": "Your focus on {area_1} is exactly where my experience lies.",
            "match_lead_two": "Your focus on {area_1} and {area_2} is exactly where my experience lies.",
            "closing_variants": [
                "My CV is attached. Could an internship on these dates fit your plans?",
                "My CV is attached with the details. Would these dates work for your team?",
            ],
            "sign_off": "Thank you,\n{applicant_name}",
        },
        "fr": {
            "subject": "Candidature spontanée — stage à partir de <<start_date>>",
            "intro_with_hook": "Je suis <<identity>> et je vous adresse une candidature spontanée : c'est votre travail sur {hook} qui m'a donné envie d'écrire à {company}.",
            "intro_standard_variants": [
                "Je suis <<identity>> et je vous adresse une candidature spontanée pour un stage au sein de {company}.",
                "Je suis <<identity>> et je souhaite proposer ma candidature spontanée pour un stage chez {company}.",
            ],
            "match_lead_one": "Votre expertise en {area_1} correspond exactement à mon expérience.",
            "match_lead_two": "Votre expertise en {area_1} et en {area_2} correspond exactement à mon expérience.",
            "closing_variants": [
                "Mon CV est en pièce jointe. Un stage à ces dates pourrait-il s'inscrire dans vos projets ?",
                "Vous trouverez mon CV en pièce jointe. Ces dates pourraient-elles convenir à votre équipe ?",
            ],
            "sign_off": "Merci par avance,\n{applicant_name}",
        },
    },
}

# ---------------------------------------------------------------------------
# What the Profile page and the per-email style switch say about each style:
# when to use it, and only evidence that was actually published. A study is
# cited by name; nothing is presented as a "success rate" that nobody measured.
# ---------------------------------------------------------------------------

SOURCES = {
    "boomerang": ("Boomerang, 40M emails", "https://blog.boomerangapp.com/2016/02/7-tips-for-getting-more-responses-to-your-emails-with-data/"),
    "boomerang_close": ("Boomerang, 350k emails", "https://blog.boomerangapp.com/2017/01/how-to-end-an-email-email-sign-offs/"),
    "backlinko": ("Backlinko & Pitchbox, 12M emails", "https://backlinko.com/email-outreach-study"),
    "jason": ("Jason Chen, CRV", "https://medium.com/@venturetwins/from-cold-email-to-internship-with-jason-chen-1cff6d560ae3"),
    "dares": ("DARES survey, INSEE Économie et Statistique 2022",
              "https://www.insee.fr/fr/statistiques/fichier/6530515/04_ES534-35_Lhommeau-Remy_FR.pdf"),
    "insee": ("INSEE Première n°1660", "https://www.insee.fr/fr/statistiques/2901587"),
    "resumego": ("ResumeGo hiring-manager survey", "https://resumegenius.com/blog/cover-letter-help/cover-letter-statistics"),
    "nace": ("NACE 2025 Internship Report", "https://www.naceweb.org/talent-acquisition/internships/intern-conversion-rate-hits-highest-mark-in-five-years"),
    "prosple": ("Prosple", "https://au.prosple.com/career-planning/internship-email"),
    "indeed": ("Indeed Career Guide", "https://www.indeed.com/career-advice/finding-a-job/how-to-write-email-asking-for-internship"),
    "pearl": ("Pearl Lemon placement programme", "https://medium.com/an-idea/how-to-send-cold-emails-to-get-an-internship-job-offer-936909507c77"),
    "kevin": ("Kevin Li", "https://medium.com/@kevinli1/five-tips-about-apply-internship-using-emails-a24560733a8d"),
}

TEMPLATE_GUIDE = {
    "specialist": {
        "best_for": {"en": "Most companies — whenever the research found something specific on their website.",
                     "fr": "La plupart des entreprises — dès que la recherche a trouvé un élément précis sur leur site."},
        "evidence": [
            ("backlinko", {"en": "A personalised email body gets 32.7% more replies.",
                           "fr": "Un message personnalisé obtient 32,7 % de réponses en plus."}),
            ("resumego", {"en": "78% of hiring managers can tell when an application was tailored.",
                          "fr": "78 % des recruteurs voient quand une candidature a été adaptée."}),
        ],
    },
    "concise": {
        "best_for": {"en": "Busy people at small companies, and anyone reading on a phone.",
                     "fr": "Les personnes très occupées des petites structures, et la lecture sur téléphone."},
        "evidence": [
            ("boomerang", {"en": "Emails of 50–125 words get the most replies — above 50%.",
                           "fr": "Les emails de 50 à 125 mots obtiennent le plus de réponses — plus de 50 %."}),
            ("indeed", {"en": "Keep it to two paragraphs: recruiters read many emails a day.",
                        "fr": "Deux paragraphes au plus : les recruteurs lisent beaucoup d'emails par jour."}),
        ],
    },
    "project_led": {
        "best_for": {"en": "When you have one standout project with a concrete result.",
                     "fr": "Quand un projet se démarque, avec un résultat concret."},
        "evidence": [
            ("nace", {"en": "Employers choose interns on skills and past experience before grades or major.",
                      "fr": "Les employeurs choisissent leurs stagiaires sur les compétences et l'expérience avant les notes ou la filière."}),
            ("prosple", {"en": "A concrete result beats “hardworking” or “excellent communication skills”.",
                         "fr": "Un résultat concret vaut mieux que « motivé » ou « excellent relationnel »."}),
        ],
    },
    "formal": {
        "best_for": {"en": "Large groups, banks and public institutions, where HR follows a fixed process.",
                     "fr": "Grands groupes, banques et institutions publiques, où les RH suivent une procédure fixe."},
        "evidence": [
            ("pearl", {"en": "Firms with rigid hiring usually redirect cold emails to their official process — apply on their careers site too.",
                       "fr": "Les entreprises au recrutement très cadré redirigent souvent vers leur procédure officielle — postulez aussi sur leur site carrières."}),
        ],
    },
    "research_lab": {
        "best_for": {"en": "Laboratories, R&D teams and research internships.",
                     "fr": "Laboratoires, équipes R&D et stages de recherche."},
        # No published study covers research internships specifically, so
        # none is cited.
        "evidence": [],
    },
    "conversation": {
        "best_for": {"en": "Startups, small teams, investment and consulting firms, alumni — anyone who can say yes to a chat without going through HR.",
                     "fr": "Startups, petites équipes, fonds et cabinets de conseil, anciens de votre école — toute personne qui peut accepter un échange sans passer par les RH."},
        "evidence": [
            ("jason", {"en": "485 emails like this led to 350 calls or interviews (72%) and an internship.",
                       "fr": "485 emails de ce type ont mené à 350 appels ou entretiens (72 %) et à un stage."}),
            ("boomerang", {"en": "Emails that ask 1–3 questions are 50% more likely to get a reply.",
                           "fr": "Les emails qui posent 1 à 3 questions ont 50 % de chances en plus d'obtenir une réponse."}),
        ],
    },
    "spontaneous": {
        "best_for": {"en": "Companies with no posted internship, and end-of-study internships with fixed dates (France, Switzerland).",
                     "fr": "Les entreprises sans offre publiée, et les stages de fin d'études à dates fixes (France, Suisse)."},
        "evidence": [
            ("dares", {"en": "French employers examine spontaneous applications in 68% of their recruitments; 21% of hires come from them.",
                       "fr": "Les employeurs français examinent des candidatures spontanées dans 68 % de leurs recrutements ; 21 % des embauches en viennent."}),
            ("boomerang", {"en": "Ending on a question gets 50% more replies than ending on a statement.",
                           "fr": "Finir sur une question obtient 50 % de réponses en plus qu'une simple affirmation."}),
        ],
    },
}

# Rules every style follows, and what the data says about each — shown on the
# Profile page under the styles.
PROVEN_RULES = [
    ("boomerang", {"en": "Short: 50–125 words get the most replies (above 50%); 3–4-word subject lines reply best.",
                   "fr": "Court : 50 à 125 mots obtiennent le plus de réponses (plus de 50 %) ; les objets de 3 à 4 mots fonctionnent le mieux."}),
    ("backlinko", {"en": "One follow-up raises replies by 65.8% — send it after about a week, then move on.",
                   "fr": "Une relance augmente les réponses de 65,8 % — envoyez-la après environ une semaine, puis passez à la suite."}),
    ("boomerang_close", {"en": "Closing with thanks: 62% replies vs 46% without.",
                         "fr": "Finir par un remerciement : 62 % de réponses contre 46 % sans."}),
    ("jason", {"en": "Send early in the week, early morning or just before lunch — not 2–5 pm.",
               "fr": "Envoyez en début de semaine, tôt le matin ou juste avant midi — pas entre 14 h et 17 h."}),
    ("kevin", {"en": "Volume matters: send to many companies (at least 10–20) and favour smaller ones.",
               "fr": "Le volume compte : écrivez à beaucoup d'entreprises (au moins 10 à 20), plutôt de petite taille."}),
]


def guide(template_id: str, lang: str = "en") -> dict:
    """best_for and evidence [{text, source, url}] for a style, in `lang`."""
    entry = TEMPLATE_GUIDE.get(template_id) or {}
    return {
        "best_for": (entry.get("best_for") or {}).get(lang, ""),
        "evidence": [{"text": text[lang], "source": SOURCES[key][0], "url": SOURCES[key][1]}
                     for key, text in entry.get("evidence") or []],
    }


def proven_rules(lang: str = "en") -> list:
    return [{"text": text[lang], "source": SOURCES[key][0], "url": SOURCES[key][1]}
            for key, text in PROVEN_RULES]

DEFAULT_TEMPLATE = "specialist"
_STRUCTURE_KEYS = ("layout", "strengths_budget", "include_motivation", "min_words", "max_words")


def template_choices(lang: str = "en") -> list:
    return [{"id": tid, "name": t["name"][lang], "description": t["description"][lang], **guide(tid, lang)}
            for tid, t in TEMPLATES.items()]


def _escape(text: str) -> str:
    """Profile text goes into strings that are later str.format()-ed per
    company; a literal brace in someone's CV must stay a brace (and must not
    become a format field)."""
    return (text or "").replace("{", "{{").replace("}", "}}")


_NUMBER_RE = re.compile(r"\b\d[\d,.]*\b")


def _numbers(text: str) -> set:
    return {m.group(0).replace(",", "").rstrip(".") for m in _NUMBER_RE.finditer(text or "")}


def _text(value, lang: str) -> str:
    if isinstance(value, dict):
        return (value.get(lang) or "").strip()
    return (value or "").strip()


def build_spec(facts: dict, template_id: str, lang: str) -> dict:
    """Profile facts + a template -> a composer spec for one language."""
    template = TEMPLATES.get(template_id) or TEMPLATES[DEFAULT_TEMPLATE]
    wording = copy.deepcopy(template[lang])
    fills = {
        "identity": _escape(_text(facts.get("identity"), lang)),
        "start_date": _escape(_text(facts.get("start_date"), lang)),
    }

    def render(value):
        if isinstance(value, list):
            return [render(v) for v in value]
        for key, filled in fills.items():
            value = value.replace(f"<<{key}>>", filled)
        return value

    email = {key: render(value) for key, value in wording.items()}
    for key in _STRUCTURE_KEYS:
        if key in template:
            email[key] = copy.deepcopy(template[key])
    # Hand-tuned wording guards at 120 words; a template has to fit shorter
    # CVs too, so its floor is lower (the guard still stops a stub email).
    email.setdefault("min_words", 90)
    email["default_topic"] = _text(facts.get("default_topic"), lang)
    email["motivation"] = _text(facts.get("motivation"), lang)
    email["internship_ask"] = _escape(_text(facts.get("internship_ask"), lang))

    areas = []
    for area in facts.get("areas") or []:
        evidence = _text(area.get("evidence"), lang)
        label = _text(area.get("label"), lang)
        if not (area.get("id") and label and evidence):
            continue
        built = {
            "id": area["id"],
            "label": label,
            "topic": _text(area.get("topic"), lang) or label,
            "match_description": (area.get("match_description") or label).strip(),
            "keywords": [k.lower() for k in area.get("keywords") or [] if k.strip()],
            "keywords_fr": [k.lower() for k in area.get("keywords_fr") or [] if k.strip()],
            "evidence": evidence,
            "sources": list(area.get("sources") or [area["id"]]),
        }
        if area.get("requires_any"):
            built["requires_any"] = list(area["requires_any"])
        if area.get("requires_any_fr"):
            built["requires_any_fr"] = list(area["requires_any_fr"])
        alt = _text(area.get("alt_evidence"), lang)
        if alt:
            built["alt_evidence"] = alt
            built["alt_sources"] = list(area.get("alt_sources") or [f"{area['id']}_alt"])
        areas.append(built)

    strengths = []
    for strength in facts.get("strengths") or []:
        text = _text(strength.get("text"), lang)
        if text:
            strengths.append({"id": strength.get("id") or f"s{len(strengths) + 1}",
                              "sources": list(strength.get("sources") or [strength.get("id") or text[:20]]),
                              "text": text})

    # Numbers the user approved in their own profile are the only ones an
    # email may state about them (the draft guard enforces it).
    allowed = set()
    for value in [email["motivation"], email["internship_ask"], fills["identity"], fills["start_date"],
                  email.get("ask_text", ""),
                  *(a["evidence"] for a in areas), *(a.get("alt_evidence", "") for a in areas),
                  *(s["text"] for s in strengths)]:
        allowed |= _numbers(value)

    spec = {
        "verified_facts": {"allowed_numbers": sorted(allowed)},
        "target_role": _text(facts.get("target_role"), lang),
        "email": email,
        "areas": areas,
        "strengths": strengths,
    }
    return spec


def spec_problems(spec: dict) -> list:
    """What's missing for this spec to produce an email at all."""
    problems = []
    email = spec.get("email", {})
    if "<<" in email.get("intro_with_hook", "") or not email.get("intro_with_hook"):
        problems.append("the introduction")
    if not email.get("internship_ask"):
        problems.append("what you're asking for (the internship sentence)")
    if not email.get("default_topic"):
        problems.append("your headline topic")
    if not spec.get("areas"):
        problems.append("at least one area of experience")
    if not spec.get("strengths"):
        problems.append("at least one strength")
    return problems
