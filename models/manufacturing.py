from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from .fabrication import _FAB_TOKEN


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    acme_fabrication_request_id = fields.Many2one('acme.fabrication.request', string='Fabrication Request Reference', check_company=True, copy=False, ondelete='restrict')
    acme_fabrication_line_id = fields.Many2one('acme.fabrication.line', string='Requested Product Line', check_company=True, copy=False, ondelete='restrict')
    acme_project_id = fields.Many2one(related='acme_fabrication_request_id.project_id', string='ACME Project', store=True)

    _acme_fabrication_line_unique = models.Constraint('UNIQUE(acme_fabrication_line_id)', 'A requested fabrication product line can have only one manufacturing order. Use a fresh request for replacement work.')

    @api.onchange('acme_fabrication_request_id')
    def _onchange_acme_request(self):
        if self.acme_fabrication_request_id:
            lines = self.acme_fabrication_request_id.line_ids
            self.acme_fabrication_line_id = lines if len(lines) == 1 else False
            if self.acme_fabrication_line_id:
                self._onchange_acme_line()

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        request_id = values.get('acme_fabrication_request_id') or self.env.context.get('default_acme_fabrication_request_id')
        request = self.env['acme.fabrication.request'].browse(request_id).exists()
        if request and len(request.line_ids) == 1:
            line = request.line_ids
            defaults = {
                'acme_fabrication_request_id': request.id,
                'acme_fabrication_line_id': line.id,
                'product_id': line.product_id.id,
                'product_uom_id': line.uom_id.id,
                'product_qty': line.quantity,
                'bom_id': line.bom_id.id,
                'origin': request.name,
            }
            if 'project_id' in self._fields:
                defaults['project_id'] = request.project_id.id
            values.update({key: value for key, value in defaults.items() if key in fields_list})
        return values

    @api.onchange('acme_fabrication_line_id')
    def _onchange_acme_line(self):
        if self.acme_fabrication_line_id:
            line = self.acme_fabrication_line_id
            self.acme_fabrication_request_id = line.request_id
            self.product_id = line.product_id
            self.product_uom_id = line.uom_id
            self.product_qty = line.quantity
            self.bom_id = line.bom_id
            self.origin = line.request_id.name

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            request = self.env['acme.fabrication.request'].browse(vals.get('acme_fabrication_request_id'))
            line = self.env['acme.fabrication.line'].browse(vals.get('acme_fabrication_line_id'))
            if line:
                if request and line.request_id != request:
                    raise ValidationError(_('The requested product line belongs to a different fabrication request.'))
                request = line.request_id
            if not request:
                continue
            request.check_access('read')
            self.env.cr.execute('SELECT id FROM acme_fabrication_request WHERE id = %s FOR UPDATE', [request.id])
            request.invalidate_recordset()
            if request.state != 'received':
                raise UserError(_('Only a request received by the workshop can create manufacturing orders.'))
            if not line:
                if len(request.line_ids) != 1:
                    raise UserError(_('Select the requested product line for this multi-product fabrication request.'))
                line = request.line_ids
            self.env.cr.execute('SELECT id FROM acme_fabrication_line WHERE id = %s FOR UPDATE', [line.id])
            line.invalidate_recordset()
            if not line.bom_id:
                raise UserError(_('The workshop must receive the request and prepare its native BoM first.'))
            expected = {'product_id': line.product_id.id, 'product_uom_id': line.uom_id.id, 'product_qty': line.quantity,
                        'bom_id': line.bom_id.id, 'company_id': request.company_id.id}
            if 'project_id' in self._fields:
                expected['project_id'] = request.project_id.id
            for key, value in expected.items():
                if key in vals and key != 'product_qty' and vals[key] != value:
                    raise UserError(_('The manufacturing product, quantity, unit, company and BoM must match the approved request.'))
            if 'product_qty' in vals and line.uom_id.compare(vals['product_qty'], line.quantity) != 0:
                raise UserError(_('The manufacturing product, quantity, unit, company and BoM must match the approved request.'))
            vals.update(expected, acme_fabrication_request_id=request.id, acme_fabrication_line_id=line.id, origin=request.name)
        return super().create(vals_list)

    def write(self, vals):
        for production in self.filtered('acme_fabrication_line_id'):
            line = production.acme_fabrication_line_id
            expected = {'product_id': line.product_id.id, 'product_uom_id': line.uom_id.id, 'product_qty': line.quantity,
                        'bom_id': production.bom_id.id, 'company_id': line.company_id.id,
                        'acme_fabrication_request_id': line.request_id.id, 'acme_fabrication_line_id': line.id}
            if 'project_id' in self._fields:
                expected['project_id'] = line.request_id.project_id.id
            if any(key in vals and key != 'product_qty' and vals[key] != value for key, value in expected.items()) or (
                'product_qty' in vals and line.uom_id.compare(vals['product_qty'], line.quantity) != 0
            ):
                raise UserError(_('The approved fabrication specification and request links cannot change. Partial/split production requires a separately scoped request.'))
        if {'acme_fabrication_request_id', 'acme_fabrication_line_id'}.intersection(vals) and any(not mo.acme_fabrication_line_id for mo in self):
            raise UserError(_('Select a fabrication reference while creating a new manufacturing order, not on an existing order.'))
        return super().write(vals)


class MrpBom(models.Model):
    _inherit = 'mrp.bom'

    acme_request_line_id = fields.Many2one('acme.fabrication.line', readonly=True, copy=False, ondelete='restrict')

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get('acme_fab_token') is not _FAB_TOKEN and any(vals.get('acme_request_line_id') for vals in vals_list):
            raise UserError(_('Fabrication BoMs are generated when an approved request is received.'))
        return super().create(vals_list)

    def write(self, vals):
        spec_fields = {'product_id', 'product_tmpl_id', 'product_qty', 'product_uom_id', 'bom_line_ids', 'type', 'company_id', 'active', 'acme_request_line_id'}
        if spec_fields.intersection(vals) and self.filtered('acme_request_line_id') and self.env.context.get('acme_fab_token') is not _FAB_TOKEN:
            raise UserError(_('A BoM generated from an approved fabrication request cannot be changed.'))
        return super().write(vals)

    def unlink(self):
        if self.filtered('acme_request_line_id'):
            raise UserError(_('An approved fabrication BoM cannot be deleted.'))
        return super().unlink()


class MrpBomLine(models.Model):
    _inherit = 'mrp.bom.line'

    def _check_acme_bom(self, bom):
        if bom.filtered('acme_request_line_id') and self.env.context.get('acme_fab_token') is not _FAB_TOKEN:
            raise UserError(_('Components of an approved fabrication BoM cannot be changed.'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('bom_id'):
                self._check_acme_bom(self.env['mrp.bom'].browse(vals['bom_id']))
        return super().create(vals_list)

    def write(self, vals):
        self._check_acme_bom(self.mapped('bom_id'))
        if vals.get('bom_id'):
            self._check_acme_bom(self.env['mrp.bom'].browse(vals['bom_id']))
        return super().write(vals)

    def unlink(self):
        self._check_acme_bom(self.mapped('bom_id'))
        return super().unlink()


class ProjectFabrication(models.Model):
    _inherit = 'project.project'

    def action_acme_fabrication(self):
        self.ensure_one()
        self.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': _('Fabrication Requests'), 'res_model': 'acme.fabrication.request', 'view_mode': 'list,form', 'domain': [('project_id', '=', self.id)], 'context': {'default_project_id': self.id}}


class WorkshopLabourCost(models.Model):
    _inherit = 'mrp.workcenter.productivity'

    acme_booked_hourly_cost = fields.Float(readonly=True, copy=False)
    acme_hourly_cost_booked = fields.Boolean(readonly=True, copy=False)

    def init(self):
        super().init()
        self.env.cr.execute("""
            UPDATE mrp_workcenter_productivity AS log
               SET acme_booked_hourly_cost = COALESCE(log.employee_cost, 0),
                   acme_hourly_cost_booked = TRUE
              FROM mrp_workorder AS work, mrp_production AS production
             WHERE work.id = log.workorder_id AND production.id = work.production_id
               AND production.project_id IS NOT NULL AND log.employee_id IS NOT NULL
               AND NOT COALESCE(log.acme_hourly_cost_booked, FALSE)
        """)

    @api.depends('acme_booked_hourly_cost', 'acme_hourly_cost_booked')
    def _compute_employee_cost(self):
        booked = self.filtered('acme_hourly_cost_booked')
        super(WorkshopLabourCost, self - booked)._compute_employee_cost()
        for log in booked:
            log.employee_cost = log.acme_booked_hourly_cost

    @api.model_create_multi
    def create(self, vals_list):
        fields_to_lock = {'acme_booked_hourly_cost', 'acme_hourly_cost_booked'}
        if any(fields_to_lock.intersection(vals) for vals in vals_list) or any(
            'default_' + key in self.env.context for key in fields_to_lock
        ):
            raise UserError(_('Project workshop rates are recorded from the native worker cost.'))
        logs = super().create(vals_list)
        for log in logs.filtered(lambda row: row.employee_id and row.workorder_id.production_id.project_id):
            super(WorkshopLabourCost, log).write({
                'acme_booked_hourly_cost': log.employee_cost, 'acme_hourly_cost_booked': True,
            })
        return logs

    def write(self, vals):
        if {'acme_booked_hourly_cost', 'acme_hourly_cost_booked'}.intersection(vals):
            raise UserError(_('The booked workshop labour rate cannot be edited.'))
        return super().write(vals)
