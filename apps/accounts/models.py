from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

from apps.core.managers import TenantManager, UnscopedManager
from apps.core.models import TenantModel, TimeStampedModel, UUIDModel


class Permiso(models.Model):
    """Permiso global del sistema. No depende del tenant: el catalogo es fijo."""

    codigo = models.CharField(max_length=100, unique=True)
    nombre = models.CharField(max_length=200)
    descripcion = models.TextField(blank=True)

    class Meta:
        verbose_name = "Permiso"
        verbose_name_plural = "Permisos"
        ordering = ["codigo"]

    def __str__(self) -> str:
        return self.codigo


class Rol(TenantModel):
    """Dueño, encargado, cajero, seguridad, promotor (seccion 3)."""

    codigo = models.SlugField(max_length=60)
    nombre = models.CharField(max_length=120)
    permisos = models.ManyToManyField(
        Permiso, through="RolPermiso", related_name="roles", blank=True
    )

    class Meta:
        verbose_name = "Rol"
        verbose_name_plural = "Roles"
        ordering = ["nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "codigo"], name="rol_codigo_unico_por_tenant"
            )
        ]

    def __str__(self) -> str:
        return self.nombre

    def codigos_de_permisos(self) -> set:
        return set(self.permisos.values_list("codigo", flat=True))


class RolPermiso(models.Model):
    rol = models.ForeignKey(Rol, on_delete=models.CASCADE, related_name="rol_permisos")
    permiso = models.ForeignKey(
        Permiso, on_delete=models.CASCADE, related_name="permiso_roles"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["rol", "permiso"], name="rol_permiso_unico"
            )
        ]


class UsuarioManager(BaseUserManager):
    """Manager del usuario.

    El usuario puede pertenecer a un tenant o a la plataforma (tenant nulo).
    Por eso no usa el TenantManager fail-closed: la autenticacion ocurre antes
    de que exista contexto de tenant.
    """

    use_in_migrations = True

    def _crear(self, email, password, **extra):
        if email:
            email = self.normalize_email(email)
        usuario = self.model(email=email, **extra)
        if password:
            usuario.set_password(password)
        else:
            usuario.set_unusable_password()
        usuario.save(using=self._db)
        return usuario

    def create_user(self, email=None, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._crear(email, password, **extra)

    def create_superuser(self, email=None, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self._crear(email, password, **extra)


class Usuario(AbstractBaseUser, PermissionsMixin, UUIDModel, TimeStampedModel):
    """Usuario operativo o administrativo.

    Dos credenciales distintas, con propositos distintos (seccion 4):

    - email + password: administracion, desde una computadora.
    - numero + PIN: operacion, desde una tablet a las 3 AM. Nadie tipea una
      contrasena larga en una barra con gente esperando.

    El PIN identifica a una PERSONA, nunca a un puesto: un login compartido
    "Barra 1" destruye la atribucion, que es la base de todo el control de
    mermas (seccion 11 ter).
    """

    tenant = models.ForeignKey(
        "tenancy.Tenant",
        on_delete=models.PROTECT,
        related_name="usuarios",
        null=True,
        blank=True,
        verbose_name="Boliche",
        help_text="Nulo para usuarios de plataforma.",
    )
    numero = models.CharField(
        "Numero de operador",
        max_length=10,
        blank=True,
        help_text="Se usa junto al PIN para ingresar en una terminal.",
    )
    email = models.EmailField(null=True, blank=True, unique=True)
    nombre = models.CharField(max_length=150)
    apellido = models.CharField(max_length=150, blank=True)

    pin_hash = models.CharField(max_length=128, blank=True)
    pin_intentos = models.PositiveSmallIntegerField(default=0)
    pin_bloqueado_hasta = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    objects = UsuarioManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = "Usuario"
        verbose_name_plural = "Usuarios"
        ordering = ["nombre", "apellido"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "numero"],
                condition=~models.Q(numero=""),
                name="usuario_numero_unico_por_tenant",
            )
        ]

    def __str__(self) -> str:
        return self.nombre_completo

    @property
    def nombre_completo(self) -> str:
        return f"{self.nombre} {self.apellido}".strip() or (self.email or str(self.id))

    # --- PIN -------------------------------------------------------------

    def set_pin(self, pin: str) -> None:
        self.pin_hash = make_password(pin)
        self.pin_intentos = 0
        self.pin_bloqueado_hasta = None

    def check_pin(self, pin: str) -> bool:
        if not self.pin_hash:
            return False
        return check_password(pin, self.pin_hash)

    def esta_bloqueado(self) -> bool:
        if self.pin_bloqueado_hasta is None:
            return False
        return self.pin_bloqueado_hasta > timezone.now()

    def registrar_pin_fallido(self) -> None:
        """Suma un intento y bloquea al llegar al maximo (seccion 4 bis)."""
        self.pin_intentos += 1
        if self.pin_intentos >= settings.PIN_MAX_INTENTOS:
            self.pin_bloqueado_hasta = timezone.now() + timedelta(
                minutes=settings.PIN_BLOQUEO_MINUTOS
            )
            self.pin_intentos = 0
        self.save(update_fields=["pin_intentos", "pin_bloqueado_hasta"])

    def registrar_pin_correcto(self) -> None:
        if self.pin_intentos or self.pin_bloqueado_hasta:
            self.pin_intentos = 0
            self.pin_bloqueado_hasta = None
            self.save(update_fields=["pin_intentos", "pin_bloqueado_hasta"])

    def permisos_efectivos(self) -> set:
        """Union de permisos de todos sus roles."""
        if self.is_superuser:
            return set(Permiso.objects.values_list("codigo", flat=True))
        return set(
            Permiso.objects.filter(permiso_roles__rol__membresias__usuario=self)
            .values_list("codigo", flat=True)
            .distinct()
        )

    def tiene_permiso(self, codigo: str) -> bool:
        return self.is_superuser or codigo in self.permisos_efectivos()


class MembresiaUsuario(TenantModel):
    """Usuario x local x rol.

    Un encargado de la sede A no ve la sede B (seccion 3).
    """

    usuario = models.ForeignKey(
        Usuario, on_delete=models.CASCADE, related_name="membresias"
    )
    local = models.ForeignKey(
        "tenancy.Local", on_delete=models.CASCADE, related_name="membresias"
    )
    rol = models.ForeignKey(Rol, on_delete=models.PROTECT, related_name="membresias")
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Membresia"
        verbose_name_plural = "Membresias"
        constraints = [
            models.UniqueConstraint(
                fields=["usuario", "local", "rol"], name="membresia_unica"
            )
        ]

    def __str__(self) -> str:
        return f"{self.usuario} en {self.local} como {self.rol}"


class AuditoriaLog(UUIDModel, TimeStampedModel):
    """Rastro de auditoria: quien, que, cuando, y con que valores.

    El riesgo no es el error, es el error invisible (seccion 7). Por eso se
    guardan los valores antes y despues, y nada se borra.
    """

    class Accion(models.TextChoices):
        CREAR = "crear", "Crear"
        ACTUALIZAR = "actualizar", "Actualizar"
        ELIMINAR = "eliminar", "Eliminar"
        ANULAR = "anular", "Anular"
        LOGIN = "login", "Ingreso"
        LOGOUT = "logout", "Salida"
        LOGIN_FALLIDO = "login_fallido", "Ingreso fallido"

    # Nullable: una accion de plataforma no tiene boliche.
    tenant = models.ForeignKey(
        "tenancy.Tenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    usuario = models.ForeignKey(
        Usuario, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    terminal = models.ForeignKey(
        "tenancy.Terminal", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    accion = models.CharField(max_length=20, choices=Accion.choices)
    entidad = models.CharField(max_length=120, help_text="Etiqueta del modelo tocado.")
    entidad_id = models.CharField(max_length=64, blank=True)
    datos_antes = models.JSONField(null=True, blank=True)
    datos_despues = models.JSONField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)

    objects = TenantManager()
    unscoped = UnscopedManager()

    class Meta:
        verbose_name = "Registro de auditoria"
        verbose_name_plural = "Registros de auditoria"
        ordering = ["-creado_en"]
        base_manager_name = "unscoped"
        default_manager_name = "objects"
        indexes = [
            models.Index(fields=["tenant", "-creado_en"]),
            models.Index(fields=["entidad", "entidad_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.accion} {self.entidad} {self.entidad_id}".strip()
