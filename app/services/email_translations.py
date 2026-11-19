# ruff: noqa: RUF001 - Arabic and Turkish letters are the content, not look-alikes.
"""The ar / fr / tr texts of every transactional e-mail (FEB-09, ADR-201).

DRAFTS. Every text here is unreviewed (FEQ-08) and marked so by `REVIEWED`;
the owner's review is listed in the stage report, not done. English is not in
this module: it stays the code in `email_templates.render`, byte for byte, and
is what an account without a language receives.

The same rules as the English texts hold, and the renderer enforces them:
subjects are constants; every value is escaped into the HTML and wrapped in
`<bdi>` so an address, a code or a workspace name reads in its own direction
inside Arabic; every link is built by `email_templates._link`, never here. A
placeholder is a `{name}` from the template's own context keys - the tests
check that each language uses exactly the placeholders English does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.services.email_templates import EmailTemplate as T

# FEQ-08: none of these has been read by a native speaker the owner trusts.
REVIEWED: Final[dict[str, bool]] = {"ar": False, "fr": False, "tr": False}


@dataclass(frozen=True, slots=True)
class Translation:
    subject: str
    # Paragraphs in order. `{code}` alone in a paragraph is the verification
    # code's own block; any other `{name}` is a value from the context.
    paragraphs: tuple[str, ...]
    # The button's label when the template carries a link, else None.
    link_label: str | None = None


@dataclass(frozen=True, slots=True)
class Chrome:
    footer: str


CHROME: Final[dict[str, Chrome]] = {
    "ar": Chrome(footer="هذه رسالة تشغيلية من Wasla بخصوص حسابك أو مساحة عملك."),
    "fr": Chrome(
        footer="Ceci est un message transactionnel de Wasla concernant votre compte "
        "ou votre espace de travail."
    ),
    "tr": Chrome(
        footer="Bu, hesabınız veya çalışma alanınız hakkında Wasla'dan gelen bir bildirimdir."
    ),
}

_AR: Final[dict[T, Translation]] = {
    T.WORKSPACE_INVITATION: Translation(
        "تمت دعوتك إلى مساحة عمل على Wasla",
        (
            "تمت دعوتك للانضمام إلى مساحة العمل {workspace_name} على Wasla.",
            "تنتهي صلاحية الدعوة بعد 7 أيام ويمكن استخدامها مرة واحدة. إذا لم تكن "
            "تتوقعها، فتجاهل هذه الرسالة ولن يحدث شيء.",
        ),
        "قبول الدعوة",
    ),
    T.PASSWORD_RESET: Translation(
        "إعادة تعيين كلمة مرور Wasla",
        (
            "طُلبت إعادة تعيين كلمة المرور لحسابك على Wasla.",
            "تنتهي صلاحية الرابط بعد 30 دقيقة ويمكن استخدامه مرة واحدة. إذا لم تطلبه، "
            "فتجاهل هذه الرسالة - لم تتغير كلمة مرورك. لن تطلب منك Wasla كلمة مرورك "
            "عبر البريد الإلكتروني أبدًا.",
        ),
        "إعادة تعيين كلمة المرور",
    ),
    T.PASSWORD_CHANGED: Translation(
        "تم تغيير كلمة مرور Wasla",
        (
            "تم تغيير كلمة المرور لحسابك على Wasla للتو، وتم تسجيل الخروج من كل الجلسات المفتوحة.",
            "إذا كنت أنت من فعل ذلك، فلا حاجة لأي إجراء. وإن لم تكن أنت، فأعد تعيين كلمة "
            "المرور فورًا من صفحة تسجيل الدخول وتواصل مع الدعم.",
        ),
    ),
    T.REGISTRATION_ATTEMPT: Translation(
        "محاولة تسجيل باستخدام بريدك على Wasla",
        (
            "حاول شخص ما إنشاء حساب على Wasla بعنوان البريد هذا. لديك حساب بالفعل، "
            "ولم يتغير فيه شيء.",
            "إذا كنت أنت، فسجّل الدخول أو استخدم إعادة تعيين كلمة المرور من صفحة تسجيل "
            "الدخول. وإن لم تكن أنت، فيمكنك تجاهل هذا التنبيه.",
        ),
    ),
    T.SESSIONS_REVOKED: Translation(
        "تم تسجيل خروجك من كل جلسات Wasla",
        (
            "تم للتو تسجيل الخروج من كل الجلسات على حسابك في Wasla.",
            "إذا كنت أنت، فسجّل الدخول مرة أخرى للمتابعة. وإن لم تكن أنت، فغيّر كلمة "
            "المرور من صفحة تسجيل الدخول فورًا.",
        ),
    ),
    T.ACCOUNT_DISABLED: Translation(
        "تم إيقاف حسابك على Wasla",
        (
            "أوقف فريق المنصة حسابك على Wasla وتم تسجيل الخروج من كل الجلسات.",
            "إذا كنت تعتقد أن هذا خطأ، فتواصل مع الدعم.",
        ),
    ),
    T.ACCOUNT_ENABLED: Translation(
        "تمت استعادة حسابك على Wasla",
        (
            "تمت استعادة حسابك على Wasla.",
            "تبقى الجلسات السابقة للإيقاف مُسجّلًا خروجها؛ سجّل الدخول مرة أخرى للمتابعة.",
        ),
    ),
    T.ACCOUNT_DELETED: Translation(
        "تم إغلاق حسابك على Wasla",
        (
            "تم إغلاق حسابك على Wasla وتسجيل الخروج من كل الجلسات.",
            "لم يعد بإمكانك تسجيل الدخول بهذا العنوان، بما في ذلك عبر Google. لم تُحذف "
            "مساحات العمل التي كنت عضوًا فيها، ولم يتأثر أعضاؤها الآخرون.",
            "إذا لم تفعل ذلك، فتواصل مع الدعم فورًا.",
        ),
    ),
    T.EMAIL_VERIFICATION: Translation(
        "تأكيد عنوان بريدك الإلكتروني على Wasla",
        (
            "استخدم هذا الرمز لتأكيد عنوان بريدك الإلكتروني على Wasla:",
            "{code}",
            "تنتهي صلاحية الرمز بعد {expires_minutes} دقيقة ويمكن استخدامه مرة واحدة. "
            "أدخله في التبويب الذي بدأت منه. لن تطلب منك Wasla أبدًا إرسال هذا الرمز "
            "لأي شخص، لا بالبريد ولا برسالة ولا بالهاتف.",
            "إذا لم تطلب ذلك، فتجاهل هذه الرسالة. لم يتغير شيء في حسابك.",
        ),
    ),
    T.INVOICE_ISSUED: Translation(
        "فاتورتك على Wasla جاهزة",
        (
            "فاتورة مساحة عملك على Wasla جاهزة: {amount_due} {currency} عن الفترة من "
            "{period_start} إلى {period_end}.",
            "سجّل الدخول إلى مساحة عملك لعرضها. لا تحتوي هذه الرسالة على رابط دفع - "
            "لا تطلب منك Wasla الدفع عبر رابط في بريد إلكتروني أبدًا.",
        ),
    ),
    T.TRIAL_EXPIRED: Translation(
        "انتهت فترتك التجريبية على Wasla",
        (
            "انتهت الفترة التجريبية لمساحة عملك {workspace_name} على Wasla.",
            "سجّل الدخول واختر خطة لتتابع من حيث توقفت.",
        ),
    ),
    T.SUBSCRIPTION_CANCELLED: Translation(
        "تم إلغاء اشتراكك في Wasla",
        (
            "تم إلغاء اشتراك مساحة عملك {workspace_name} على Wasla.",
            "بياناتك محفوظة؛ سجّل الدخول واختر خطة في أي وقت للمتابعة.",
        ),
    ),
    T.SUBSCRIPTION_SUSPENDED: Translation(
        "تم إيقاف مساحة عملك على Wasla",
        (
            "تم إيقاف مساحة عملك {workspace_name} على Wasla لأن فاتورة بقيمة "
            "{amount_due} {currency} ما زالت غير مدفوعة.",
            "بياناتك محفوظة ولم يُحذف شيء. سجّل الدخول وسدّد الفاتورة المستحقة لاستعادة الخدمة.",
            "لا تحتوي هذه الرسالة على رابط دفع - لا تطلب منك Wasla الدفع عبر رابط في "
            "بريد إلكتروني أبدًا.",
        ),
    ),
    T.CHANNEL_CAPACITY_SCHEDULED: Translation(
        "تغيير خطتك على Wasla سيتسع لقنوات أقل",
        (
            "يسري تغيير الخطة المجدول لمساحة عملك {workspace_name} على Wasla في "
            "{effective_at}. تتسع الخطة لـ {capacity} من اتصالات القنوات، والمتصل الآن {active}.",
            "لا يتغير شيء قبل ذلك الموعد. سجّل الدخول لتختار الاتصالات التي تبقيها؛ "
            "وإن لم تفعل، فسيكون لديك {grace_days} يومًا من ذلك التاريخ للاختيار.",
        ),
    ),
    T.CHANNEL_CAPACITY_REDUCTION_STARTED: Translation(
        "اختر اتصالات القنوات التي تبقيها على Wasla",
        (
            "أصبحت مساحة عملك {workspace_name} على Wasla تتسع لـ {capacity} من "
            "اتصالات القنوات، والمتصل {active}.",
            "يستمر كل اتصال في العمل حتى {grace_ends_at}. سجّل الدخول واختر ما تبقيه "
            "قبل ذلك؛ ستُعطَّل الاتصالات الأخرى دون أن تُحذف أبدًا، ويمكنك تفعيل أي منها "
            "مجددًا متى توفرت خانة.",
            "إذا لم يختر أحد، تُبقي Wasla الأقدم.",
        ),
    ),
    T.CHANNEL_CAPACITY_REDUCTION_WARNING: Translation(
        "ستختار Wasla اتصالات قنواتك خلال يومين",
        (
            "في {grace_ends_at} ستختار Wasla اتصالات القنوات التي تبقيها مساحة عملك "
            "{workspace_name}: الأقدم، والقنوات التي تشملها خطتها فقط.",
            "سجّل الدخول قبل ذلك لتختار بنفسك. لا يُحذف شيء أبدًا.",
        ),
    ),
}

_FR: Final[dict[T, Translation]] = {
    T.WORKSPACE_INVITATION: Translation(
        "Vous êtes invité dans un espace de travail sur Wasla",
        (
            "Vous êtes invité à rejoindre l'espace de travail {workspace_name} sur Wasla.",
            "L'invitation expire dans 7 jours et ne peut être utilisée qu'une fois. Si "
            "vous ne l'attendiez pas, ignorez ce message et rien ne se passera.",
        ),
        "Accepter l'invitation",
    ),
    T.PASSWORD_RESET: Translation(
        "Réinitialisez votre mot de passe Wasla",
        (
            "Une réinitialisation du mot de passe a été demandée pour votre compte Wasla.",
            "Le lien expire dans 30 minutes et ne peut être utilisé qu'une fois. Si vous "
            "ne l'avez pas demandée, ignorez ce message - votre mot de passe n'a pas "
            "changé. Wasla ne vous demandera jamais votre mot de passe par e-mail.",
        ),
        "Réinitialiser le mot de passe",
    ),
    T.PASSWORD_CHANGED: Translation(
        "Votre mot de passe Wasla a été modifié",
        (
            "Le mot de passe de votre compte Wasla vient d'être modifié, et toutes les "
            "sessions ouvertes ont été fermées.",
            "Si c'était vous, il n'y a rien d'autre à faire. Sinon, réinitialisez "
            "immédiatement votre mot de passe depuis la page de connexion et contactez "
            "le support.",
        ),
    ),
    T.REGISTRATION_ATTEMPT: Translation(
        "Une inscription a été tentée avec votre adresse Wasla",
        (
            "Quelqu'un a tenté de créer un compte Wasla avec cette adresse e-mail. Vous "
            "avez déjà un compte, et rien n'y a été modifié.",
            "Si c'était vous, connectez-vous ou utilisez la réinitialisation du mot de "
            "passe depuis la page de connexion. Sinon, vous pouvez ignorer ce message.",
        ),
    ),
    T.SESSIONS_REVOKED: Translation(
        "Vous avez été déconnecté de toutes vos sessions Wasla",
        (
            "Toutes les sessions de votre compte Wasla viennent d'être fermées.",
            "Si c'était vous, reconnectez-vous pour continuer. Sinon, changez "
            "immédiatement votre mot de passe depuis la page de connexion.",
        ),
    ),
    T.ACCOUNT_DISABLED: Translation(
        "Votre compte Wasla a été suspendu",
        (
            "Votre compte Wasla a été suspendu par l'équipe de la plateforme et toutes "
            "les sessions ont été fermées.",
            "Si vous pensez qu'il s'agit d'une erreur, contactez le support.",
        ),
    ),
    T.ACCOUNT_ENABLED: Translation(
        "Votre compte Wasla a été rétabli",
        (
            "Votre compte Wasla a été rétabli.",
            "Les sessions antérieures à la suspension restent fermées ; reconnectez-vous "
            "pour continuer.",
        ),
    ),
    T.ACCOUNT_DELETED: Translation(
        "Votre compte Wasla a été fermé",
        (
            "Votre compte Wasla a été fermé et toutes les sessions ont été fermées.",
            "Vous ne pouvez plus vous connecter avec cette adresse, y compris via "
            "Google. Les espaces de travail dont vous étiez membre n'ont pas été "
            "supprimés, et leurs autres membres ne sont pas concernés.",
            "Si vous n'êtes pas à l'origine de cette action, contactez immédiatement le support.",
        ),
    ),
    T.EMAIL_VERIFICATION: Translation(
        "Vérifiez votre adresse e-mail pour Wasla",
        (
            "Utilisez ce code pour vérifier votre adresse e-mail sur Wasla :",
            "{code}",
            "Le code expire dans {expires_minutes} minutes et ne peut être utilisé "
            "qu'une fois. Saisissez-le dans l'onglet d'où vous êtes parti. Wasla ne vous "
            "demandera jamais d'envoyer ce code à qui que ce soit, par e-mail, message "
            "ou téléphone.",
            "Si vous ne l'avez pas demandé, ignorez ce message. Rien n'a changé sur votre compte.",
        ),
    ),
    T.INVOICE_ISSUED: Translation(
        "Votre facture Wasla est prête",
        (
            "Une facture pour votre espace de travail Wasla est prête : {amount_due} "
            "{currency} pour la période du {period_start} au {period_end}.",
            "Connectez-vous à votre espace de travail pour la consulter. Ce message ne "
            "contient aucun lien de paiement - Wasla ne vous demande jamais de payer via "
            "un lien dans un e-mail.",
        ),
    ),
    T.TRIAL_EXPIRED: Translation(
        "Votre essai Wasla est terminé",
        (
            "L'essai de votre espace de travail {workspace_name} sur Wasla est terminé.",
            "Connectez-vous et choisissez une offre pour reprendre là où vous en étiez.",
        ),
    ),
    T.SUBSCRIPTION_CANCELLED: Translation(
        "Votre abonnement Wasla a été résilié",
        (
            "L'abonnement de votre espace de travail {workspace_name} sur Wasla a été résilié.",
            "Vos données sont conservées ; connectez-vous et choisissez une offre à tout "
            "moment pour continuer.",
        ),
    ),
    T.SUBSCRIPTION_SUSPENDED: Translation(
        "Votre espace de travail Wasla a été suspendu",
        (
            "Votre espace de travail {workspace_name} sur Wasla a été suspendu car une "
            "facture de {amount_due} {currency} reste impayée.",
            "Vos données sont conservées et rien n'a été supprimé. Connectez-vous et "
            "réglez la facture en attente pour rétablir le service.",
            "Ce message ne contient aucun lien de paiement - Wasla ne vous demande "
            "jamais de payer via un lien dans un e-mail.",
        ),
    ),
    T.CHANNEL_CAPACITY_SCHEDULED: Translation(
        "Le changement d'offre Wasla comptera moins de canaux",
        (
            "Le changement d'offre prévu pour votre espace de travail {workspace_name} "
            "sur Wasla prend effet le {effective_at}. Il permet {capacity} connexions de "
            "canaux, et {active} sont connectées aujourd'hui.",
            "Rien ne change d'ici là. Connectez-vous pour choisir les connexions à "
            "garder ; sinon, vous aurez {grace_days} jours à partir de cette date pour choisir.",
        ),
    ),
    T.CHANNEL_CAPACITY_REDUCTION_STARTED: Translation(
        "Choisissez les connexions de canaux Wasla à garder",
        (
            "Votre espace de travail {workspace_name} sur Wasla permet désormais "
            "{capacity} connexions de canaux, et {active} sont connectées.",
            "Chaque connexion fonctionne jusqu'au {grace_ends_at}. Connectez-vous et "
            "choisissez celles à garder d'ici là ; les autres seront désactivées, jamais "
            "supprimées, et vous pourrez en réactiver une dès qu'une place se libère.",
            "Si personne ne choisit, Wasla garde les plus anciennes.",
        ),
    ),
    T.CHANNEL_CAPACITY_REDUCTION_WARNING: Translation(
        "Wasla choisira vos connexions de canaux dans deux jours",
        (
            "Le {grace_ends_at}, Wasla choisira les connexions de canaux que garde votre "
            "espace de travail {workspace_name} : les plus anciennes, et uniquement les "
            "canaux inclus dans son offre.",
            "Connectez-vous d'ici là pour choisir vous-même. Rien n'est jamais supprimé.",
        ),
    ),
}

_TR: Final[dict[T, Translation]] = {
    T.WORKSPACE_INVITATION: Translation(
        "Wasla'da bir çalışma alanına davet edildiniz",
        (
            "Wasla'daki {workspace_name} çalışma alanına katılmaya davet edildiniz.",
            "Davet 7 gün içinde sona erer ve yalnızca bir kez kullanılabilir. "
            "Beklemiyorsanız bu iletiyi yok sayın; hiçbir şey olmaz.",
        ),
        "Daveti kabul et",
    ),
    T.PASSWORD_RESET: Translation(
        "Wasla parolanızı sıfırlayın",
        (
            "Wasla hesabınız için parola sıfırlama istendi.",
            "Bağlantı 30 dakika içinde sona erer ve yalnızca bir kez kullanılabilir. "
            "Siz istemediyseniz bu iletiyi yok sayın - parolanız değişmedi. Wasla "
            "parolanızı asla e-postayla sormaz.",
        ),
        "Parolayı sıfırla",
    ),
    T.PASSWORD_CHANGED: Translation(
        "Wasla parolanız değiştirildi",
        (
            "Wasla hesabınızın parolası az önce değiştirildi ve açık oturumların tümü kapatıldı.",
            "Bunu siz yaptıysanız başka bir şey gerekmez. Siz yapmadıysanız giriş "
            "sayfasından parolanızı hemen sıfırlayın ve destekle iletişime geçin.",
        ),
    ),
    T.REGISTRATION_ATTEMPT: Translation(
        "Wasla e-postanızla bir kayıt denendi",
        (
            "Biri bu e-posta adresiyle bir Wasla hesabı oluşturmayı denedi. Zaten bir "
            "hesabınız var ve üzerinde hiçbir şey değişmedi.",
            "Bu sizseniz giriş yapın veya giriş sayfasından parola sıfırlamayı kullanın. "
            "Siz değilseniz bu bildirimi yok sayabilirsiniz.",
        ),
    ),
    T.SESSIONS_REVOKED: Translation(
        "Tüm Wasla oturumlarınız kapatıldı",
        (
            "Wasla hesabınızdaki tüm oturumlar az önce kapatıldı.",
            "Bu sizseniz devam etmek için yeniden giriş yapın. Siz değilseniz giriş "
            "sayfasından parolanızı hemen değiştirin.",
        ),
    ),
    T.ACCOUNT_DISABLED: Translation(
        "Wasla hesabınız askıya alındı",
        (
            "Wasla hesabınız platform ekibi tarafından askıya alındı ve tüm oturumlar kapatıldı.",
            "Bunun bir hata olduğunu düşünüyorsanız destekle iletişime geçin.",
        ),
    ),
    T.ACCOUNT_ENABLED: Translation(
        "Wasla hesabınız geri açıldı",
        (
            "Wasla hesabınız geri açıldı.",
            "Askıya almadan önceki oturumlar kapalı kalır; devam etmek için yeniden giriş yapın.",
        ),
    ),
    T.ACCOUNT_DELETED: Translation(
        "Wasla hesabınız kapatıldı",
        (
            "Wasla hesabınız kapatıldı ve tüm oturumlar sonlandırıldı.",
            "Bu adresle artık Google dahil hiçbir yolla giriş yapamazsınız. Üyesi "
            "olduğunuz çalışma alanları silinmedi ve diğer üyeleri etkilenmedi.",
            "Bunu siz yapmadıysanız hemen destekle iletişime geçin.",
        ),
    ),
    T.EMAIL_VERIFICATION: Translation(
        "Wasla için e-posta adresinizi doğrulayın",
        (
            "Wasla'da e-posta adresinizi doğrulamak için bu kodu kullanın:",
            "{code}",
            "Kod {expires_minutes} dakika içinde sona erer ve yalnızca bir kez "
            "kullanılabilir. Başladığınız sekmeye girin. Wasla bu kodu hiç kimseye "
            "e-posta, mesaj veya telefonla göndermenizi asla istemez.",
            "Bunu siz istemediyseniz bu iletiyi yok sayın. Hesabınızda hiçbir şey değişmedi.",
        ),
    ),
    T.INVOICE_ISSUED: Translation(
        "Wasla faturanız hazır",
        (
            "Wasla çalışma alanınızın faturası hazır: {period_start} - {period_end} "
            "dönemi için {amount_due} {currency}.",
            "Görüntülemek için çalışma alanınıza giriş yapın. Bu iletide ödeme bağlantısı "
            "yoktur - Wasla sizden asla e-postadaki bir bağlantıyla ödeme yapmanızı istemez.",
        ),
    ),
    T.TRIAL_EXPIRED: Translation(
        "Wasla deneme süreniz sona erdi",
        (
            "Wasla'daki {workspace_name} çalışma alanınızın deneme süresi sona erdi.",
            "Kaldığınız yerden devam etmek için giriş yapıp bir plan seçin.",
        ),
    ),
    T.SUBSCRIPTION_CANCELLED: Translation(
        "Wasla aboneliğiniz iptal edildi",
        (
            "Wasla'daki {workspace_name} çalışma alanınızın aboneliği iptal edildi.",
            "Verileriniz saklanıyor; devam etmek için istediğiniz zaman giriş yapıp "
            "bir plan seçin.",
        ),
    ),
    T.SUBSCRIPTION_SUSPENDED: Translation(
        "Wasla çalışma alanınız askıya alındı",
        (
            "Wasla'daki {workspace_name} çalışma alanınız, {amount_due} {currency} "
            "tutarındaki bir fatura hâlâ ödenmediği için askıya alındı.",
            "Verileriniz saklanıyor ve hiçbir şey silinmedi. Hizmeti geri açmak için "
            "giriş yapıp bekleyen faturayı ödeyin.",
            "Bu iletide ödeme bağlantısı yoktur - Wasla sizden asla e-postadaki bir "
            "bağlantıyla ödeme yapmanızı istemez.",
        ),
    ),
    T.CHANNEL_CAPACITY_SCHEDULED: Translation(
        "Wasla plan değişikliğiniz daha az kanal alacak",
        (
            "Wasla'daki {workspace_name} çalışma alanınız için planlanan plan değişikliği "
            "{effective_at} tarihinde geçerli olur. {capacity} kanal bağlantısı alır ve "
            "şu anda {active} bağlı.",
            "O zamana kadar hiçbir şey değişmez. Hangi bağlantıları tutacağınızı seçmek "
            "için giriş yapın; seçmezseniz o tarihten itibaren seçmek için {grace_days} "
            "gününüz olur.",
        ),
    ),
    T.CHANNEL_CAPACITY_REDUCTION_STARTED: Translation(
        "Tutulacak Wasla kanal bağlantılarını seçin",
        (
            "Wasla'daki {workspace_name} çalışma alanınız artık {capacity} kanal "
            "bağlantısı alıyor ve {active} bağlı.",
            "Her bağlantı {grace_ends_at} tarihine kadar çalışmaya devam eder. O zamana "
            "kadar giriş yapıp hangilerini tutacağınızı seçin; diğerleri devre dışı "
            "bırakılır, asla silinmez ve bir yer açıldığında yeniden etkinleştirebilirsiniz.",
            "Kimse seçmezse Wasla en eskileri tutar.",
        ),
    ),
    T.CHANNEL_CAPACITY_REDUCTION_WARNING: Translation(
        "Wasla iki gün içinde kanal bağlantılarınızı seçecek",
        (
            "{grace_ends_at} tarihinde Wasla, {workspace_name} çalışma alanınızın hangi "
            "kanal bağlantılarını tutacağını seçecek: en eskileri ve yalnızca planının "
            "içerdiği kanalları.",
            "Kendiniz seçmek için o zamana kadar giriş yapın. Hiçbir şey asla silinmez.",
        ),
    ),
}

TRANSLATIONS: Final[dict[str, dict[T, Translation]]] = {"ar": _AR, "fr": _FR, "tr": _TR}
