"""Entradas, control de acceso y aforo (ANALISIS.md secciones 5 y 9 bis).

Tres decisiones que definen si esto sirve en una puerta real:

1. El codigo QR es un token aleatorio de alta entropia, NUNCA el identificador de
   la entrada. Con el id, cualquiera que vea una entrada adivina las demas.
2. Una entrada se consume una sola vez, con una actualizacion condicional atomica.
   No alcanza con "leer, chequear y escribir": dos puertas simultaneas aceptarian
   la misma entrada.
3. El aforo es una PROYECCION del ledger de movimientos, no un contador mutable.
   Sobrevive a reconexiones y queda auditable, que es lo que la Intendencia mira.
"""

from __future__ import annotations

import secrets
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.managers import TenantManager, UnscopedManager
from apps.core.models import TenantModel

# 32 bytes de urgencia criptografica: adivinar un token no es viable.
LARGO_TOKEN = 32


def generar_token() -> str:
    return secrets.token_urlsafe(LARGO_TOKEN)


class Evento(TenantModel):
    """La noche concreta: fecha, aforo y precios."""

    class Estado(models.TextChoices):
        BORRADOR = "borrador", "Borrador"
        PUBLICADO = "publicado", "Publicado"
        FINALIZADO = "finalizado", "Finalizado"
        CANCELADO = "cancelado", "Cancelado"

    local = models.ForeignKey(
        "tenancy.Local", on_delete=models.PROTECT, related_name="eventos"
    )
    nombre = models.CharField(max_length=150)
    fecha = models.DateField()
    hora_apertura = models.TimeField(null=True, blank=True)
    hora_cierre = models.TimeField(null=True, blank=True)
    aforo = models.PositiveIntegerField(
        help_text="Capacidad del local para este evento, tomada de la habilitacion."
    )
    estado = models.CharField(
        max_length=16, choices=Estado.choices, default=Estado.BORRADOR
    )

    class Meta:
        verbose_name = "Evento"
        verbose_name_plural = "Eventos"
        ordering = ["-fecha", "nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "local", "fecha", "nombre"],
                name="evento_unico_por_local_y_fecha",
            )
        ]

    def __str__(self) -> str:
        return f"{self.nombre} ({self.fecha})"


class TipoEntrada(TenantModel):
    """General, Damas, VIP... cada uno con su precio y su cupo."""

    evento = models.ForeignKey(
        Evento, on_delete=models.CASCADE, related_name="tipos"
    )
    nombre = models.CharField(max_length=80)
    precio = models.DecimalField(max_digits=12, decimal_places=2)
    cupo = models.PositiveIntegerField(
        help_text="Cuantas entradas de este tipo se pueden vender."
    )
    orden = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "Tipo de entrada"
        verbose_name_plural = "Tipos de entrada"
        ordering = ["evento", "orden", "nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "evento", "nombre"], name="tipo_entrada_unico"
            )
        ]

    def __str__(self) -> str:
        return f"{self.evento.nombre} / {self.nombre}"

    @property
    def vendidas(self) -> int:
        return Entrada.objects.filter(
            tipo=self, estado__in=[Entrada.Estado.EMITIDA, Entrada.Estado.USADA]
        ).count()

    @property
    def disponibles(self) -> int:
        return max(0, self.cupo - self.vendidas)


class Entrada(TenantModel):
    """Una entrada emitida."""

    class Estado(models.TextChoices):
        EMITIDA = "emitida", "Emitida"
        USADA = "usada", "Usada"
        ANULADA = "anulada", "Anulada"

    class Canal(models.TextChoices):
        PUERTA = "puerta", "Puerta"
        ONLINE = "online", "Online"
        INVITACION = "invitacion", "Invitacion"

    evento = models.ForeignKey(
        Evento, on_delete=models.PROTECT, related_name="entradas"
    )
    tipo = models.ForeignKey(
        TipoEntrada, on_delete=models.PROTECT, related_name="entradas"
    )
    folio = models.CharField(max_length=24)
    precio = models.DecimalField(max_digits=12, decimal_places=2)
    estado = models.CharField(
        max_length=16, choices=Estado.choices, default=Estado.EMITIDA
    )
    # Token del QR: aleatorio, no derivado del id, y unico.
    qr_token = models.CharField(max_length=64, unique=True, default=generar_token)
    canal = models.CharField(
        max_length=16, choices=Canal.choices, default=Canal.PUERTA
    )
    comprador_nombre = models.CharField(max_length=150, blank=True)
    comprador_contacto = models.CharField(max_length=120, blank=True)
    venta = models.ForeignKey(
        "sales.Venta",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="entradas",
    )
    # Compra online: agrupa las entradas de una misma reserva, para que quien
    # compro cuatro vea las cuatro y no solo una.
    reserva = models.ForeignKey(
        "ticketing.Reserva",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="entradas",
    )
    usada_en = models.DateTimeField(null=True, blank=True)
    usada_en_terminal = models.ForeignKey(
        "tenancy.Terminal",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="entradas_validadas",
    )
    usada_por = models.ForeignKey(
        "accounts.Usuario",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="entradas_validadas",
    )
    anulada_en = models.DateTimeField(null=True, blank=True)
    anulada_por = models.ForeignKey(
        "accounts.Usuario",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="entradas_anuladas",
    )
    motivo_anulacion = models.CharField(max_length=250, blank=True)

    class Meta:
        verbose_name = "Entrada"
        verbose_name_plural = "Entradas"
        ordering = ["-creado_en"]
        indexes = [
            models.Index(fields=["tenant", "evento", "estado"]),
            models.Index(fields=["tenant", "qr_token"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "evento", "folio"], name="entrada_folio_unico"
            )
        ]

    def __str__(self) -> str:
        return f"Entrada {self.folio} ({self.estado})"

    @property
    def puede_usarse(self) -> bool:
        return self.estado == self.Estado.EMITIDA


class MovimientoAforo(TenantModel):
    """Asiento inmutable del aforo. El aforo en vivo es su suma."""

    class Tipo(models.TextChoices):
        INGRESO = "ingreso", "Ingreso"
        EGRESO = "egreso", "Egreso"
        AJUSTE = "ajuste", "Ajuste"

    evento = models.ForeignKey(
        Evento, on_delete=models.PROTECT, related_name="movimientos_aforo"
    )
    tipo = models.CharField(max_length=16, choices=Tipo.choices)
    delta = models.IntegerField(
        help_text="Positivo suma gente adentro, negativo resta."
    )
    motivo = models.CharField(max_length=250, blank=True)
    entrada = models.ForeignKey(
        Entrada,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movimientos_aforo",
    )
    usuario = models.ForeignKey(
        "accounts.Usuario",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movimientos_aforo",
    )
    terminal = models.ForeignKey(
        "tenancy.Terminal",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movimientos_aforo",
    )

    objects = TenantManager()
    unscoped = UnscopedManager()

    class Meta:
        verbose_name = "Movimiento de aforo"
        verbose_name_plural = "Movimientos de aforo"
        ordering = ["-creado_en"]
        base_manager_name = "unscoped"
        default_manager_name = "objects"
        indexes = [models.Index(fields=["tenant", "evento", "-creado_en"])]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(delta=0), name="aforo_delta_no_cero"
            )
        ]

    def __str__(self) -> str:
        return f"{self.tipo} {self.delta:+d}"

    def clean(self):
        super().clean()
        if self.delta is None:
            return
        if self.tipo == self.Tipo.INGRESO and self.delta < 0:
            raise ValidationError({"delta": "Un ingreso no puede restar aforo."})
        if self.tipo == self.Tipo.EGRESO and self.delta > 0:
            raise ValidationError({"delta": "Un egreso no puede sumar aforo."})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "Los movimientos de aforo no se modifican: se asienta el contrario."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Los movimientos de aforo no se borran: se asienta el contrario."
        )


class Reserva(TenantModel):
    """Cupo tomado mientras el comprador paga, con vencimiento.

    Es el patron que evita la sobreventa: primero se RESERVA de forma atomica,
    despues se cobra, y recien ahi se emite la entrada. Vender "consultando si hay
    lugar y despues cobrando" sobrevende; cobrar primero y asignar despues termina
    en reembolsos.

    Referencia: ANALISIS.md seccion 9 bis.
    """

    class Estado(models.TextChoices):
        ACTIVA = "activa", "Activa"
        CONFIRMADA = "confirmada", "Confirmada"
        EXPIRADA = "expirada", "Expirada"
        CANCELADA = "cancelada", "Cancelada"

    evento = models.ForeignKey(
        Evento, on_delete=models.PROTECT, related_name="reservas"
    )
    tipo = models.ForeignKey(
        TipoEntrada, on_delete=models.PROTECT, related_name="reservas"
    )
    cantidad = models.PositiveIntegerField(default=1)
    comprador_nombre = models.CharField(max_length=150, blank=True)
    comprador_contacto = models.CharField(max_length=120, blank=True)
    estado = models.CharField(
        max_length=16, choices=Estado.choices, default=Estado.ACTIVA
    )
    token = models.CharField(max_length=64, unique=True, default=generar_token)
    expira_en = models.DateTimeField()
    confirmada_en = models.DateTimeField(null=True, blank=True)

    referencia_pago = models.CharField(max_length=120, blank=True)
    medio_pago = models.CharField(max_length=20, blank=True)
    pagado_en = models.DateTimeField(null=True, blank=True)
    # Un pago puede aprobarse DESPUES de que la reserva vencio. Se registra, se
    # devuelve y se avisa: nunca se sobrevende "para que funcione".
    pago_tardio = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Reserva"
        verbose_name_plural = "Reservas"
        ordering = ["-creado_en"]
        indexes = [
            models.Index(fields=["tenant", "estado", "expira_en"]),
            models.Index(fields=["tenant", "token"]),
        ]

    def __str__(self) -> str:
        return f"Reserva {self.cantidad} x {self.tipo.nombre} ({self.estado})"

    @property
    def vencida(self) -> bool:
        from django.utils import timezone as _tz

        return self.estado == self.Estado.ACTIVA and self.expira_en <= _tz.now()


class Promotor(TenantModel):
    """Quien trae gente a cambio de una comision.

    La comision se calcula sobre el REGISTRO del evento, nunca sobre lo que
    declara el promotor (ANALISIS.md seccion 9 ter).
    """

    class TipoComision(models.TextChoices):
        # Hoy solo se liquida por persona que ingresa, que es lo verificable: sale
        # del registro de la puerta. Una comision sobre el CONSUMO exige vincular
        # lo que gasta cada invitado a la venta, y eso es fase 3. Preferimos no
        # ofrecer un modelo de comision que no sabemos liquidar.
        POR_PERSONA = "por_persona", "Monto fijo por persona que ingresa"

    nombre = models.CharField(max_length=150)
    contacto = models.CharField(max_length=120, blank=True)
    documento = models.CharField(max_length=30, blank=True)
    tipo_comision = models.CharField(
        max_length=24, choices=TipoComision.choices, default=TipoComision.POR_PERSONA
    )
    valor_comision = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0")
    )
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Promotor"
        verbose_name_plural = "Promotores"
        ordering = ["nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "nombre"], name="promotor_nombre_unico_por_tenant"
            )
        ]

    def __str__(self) -> str:
        return self.nombre


class Lista(TenantModel):
    """Lista de invitados de un evento.

    Tres reglas que evitan que la lista se convierta en un agujero:

    1. El CORTE lo aplica el sistema, no el de la puerta: si queda a criterio del
       personal, el criterio es donde se va la plata.
    2. La ATRIBUCION ocurre en la puerta, al ingresar, y no se puede editar
       despues. Sin eso, todos los promotores reclaman a todos.
    3. La lista CONSUME AFORO: los invitados ocupan capacidad, y el aforo es lo
       que fiscaliza la Intendencia.
    """

    class Tipo(models.TextChoices):
        CASA = "casa", "Lista de la casa"
        PROMOTOR = "promotor", "Lista de promotor"
        DJ = "dj", "Lista del DJ"
        SPONSOR = "sponsor", "Sponsor"
        STAFF = "staff", "Staff"
        CUMPLEANOS = "cumpleanos", "Cumpleanos"

    class Estado(models.TextChoices):
        ABIERTA = "abierta", "Abierta"
        CERRADA = "cerrada", "Cerrada"

    evento = models.ForeignKey(
        Evento, on_delete=models.CASCADE, related_name="listas"
    )
    nombre = models.CharField(max_length=150)
    tipo = models.CharField(max_length=16, choices=Tipo.choices, default=Tipo.CASA)
    promotor = models.ForeignKey(
        Promotor,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="listas",
    )
    # Quien la creo: la auditoria anti-amigos empieza aca.
    creada_por = models.ForeignKey(
        "accounts.Usuario",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="listas_creadas",
    )
    cupo = models.PositiveIntegerField(
        help_text="Cuantas personas pueden entrar por esta lista en total."
    )
    hora_de_corte = models.TimeField(
        null=True,
        blank=True,
        help_text="Despues de esta hora la lista no vale mas. Si es nulo, vale toda la noche.",
    )
    estado = models.CharField(
        max_length=16, choices=Estado.choices, default=Estado.ABIERTA
    )

    class Meta:
        verbose_name = "Lista"
        verbose_name_plural = "Listas"
        ordering = ["evento", "nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "evento", "nombre"], name="lista_unica_por_evento"
            )
        ]

    def __str__(self) -> str:
        return f"{self.nombre} ({self.evento.nombre})"

    @property
    def usados(self) -> int:
        from django.db.models import Sum as _Sum

        total = self.usos.aggregate(t=_Sum("personas"))["t"]
        return int(total or 0)

    @property
    def disponibles(self) -> int:
        return max(0, self.cupo - self.usados)


class ListaInvitado(TenantModel):
    """Una invitacion: una persona, con N acompanantes."""

    lista = models.ForeignKey(Lista, on_delete=models.CASCADE, related_name="invitados")
    nombre = models.CharField(max_length=150)
    contacto = models.CharField(max_length=120, blank=True)
    personas_que_cubre = models.PositiveSmallIntegerField(
        default=1, help_text="1 + acompanantes."
    )
    qr_token = models.CharField(max_length=64, unique=True, default=generar_token)
    anulado = models.BooleanField(default=False)
    motivo_anulacion = models.CharField(max_length=250, blank=True)

    class Meta:
        verbose_name = "Invitado"
        verbose_name_plural = "Invitados"
        ordering = ["lista", "nombre"]
        indexes = [models.Index(fields=["tenant", "qr_token"])]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "lista", "nombre"], name="invitado_unico_por_lista"
            )
        ]

    def __str__(self) -> str:
        return f"{self.nombre} ({self.personas_que_cubre})"

    @property
    def personas_usadas(self) -> int:
        from django.db.models import Sum as _Sum

        total = self.usos.aggregate(t=_Sum("personas"))["t"]
        return int(total or 0)

    @property
    def personas_restantes(self) -> int:
        return max(0, self.personas_que_cubre - self.personas_usadas)


class UsoLista(TenantModel):
    """Ingreso registrado en la puerta.

    Es el asiento que atribuye: queda quien entro, cuantas personas, cuando, en
    que terminal y quien lo dejo pasar. No se edita ni se borra.
    """

    invitado = models.ForeignKey(
        ListaInvitado, on_delete=models.PROTECT, related_name="usos"
    )
    lista = models.ForeignKey(Lista, on_delete=models.PROTECT, related_name="usos")
    personas = models.PositiveSmallIntegerField(default=1)
    terminal = models.ForeignKey(
        "tenancy.Terminal",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="usos_de_lista",
    )
    # Quien autorizo el ingreso: es la atribucion que despues se paga.
    registrado_por = models.ForeignKey(
        "accounts.Usuario",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="usos_de_lista",
    )
    observaciones = models.CharField(max_length=250, blank=True)

    class Meta:
        verbose_name = "Uso de lista"
        verbose_name_plural = "Usos de lista"
        ordering = ["-creado_en"]

    def __str__(self) -> str:
        return f"{self.invitado.nombre} x{self.personas}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "Un uso de lista no se modifica: la atribucion se registra al "
                "ingresar y no se edita despues."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Un uso de lista no se borra.")


class ComisionPromotor(TenantModel):
    """Liquidacion de un promotor por un evento."""

    class Estado(models.TextChoices):
        CALCULADA = "calculada", "Calculada"
        APROBADA = "aprobada", "Aprobada"
        PAGADA = "pagada", "Pagada"

    evento = models.ForeignKey(
        Evento, on_delete=models.PROTECT, related_name="comisiones"
    )
    promotor = models.ForeignKey(
        Promotor, on_delete=models.PROTECT, related_name="comisiones"
    )
    personas = models.PositiveIntegerField(default=0, help_text="Personas que ingresaron.")
    base = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    monto = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    estado = models.CharField(
        max_length=16, choices=Estado.choices, default=Estado.CALCULADA
    )
    aprobada_por = models.ForeignKey(
        "accounts.Usuario",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="comisiones_aprobadas",
    )
    pagada_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Comision de promotor"
        verbose_name_plural = "Comisiones de promotores"
        ordering = ["-creado_en"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "evento", "promotor"],
                name="comision_unica_por_evento_y_promotor",
            )
        ]

    def __str__(self) -> str:
        return f"{self.promotor.nombre}: {self.monto} ({self.estado})"
