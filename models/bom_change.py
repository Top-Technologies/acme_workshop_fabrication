import math

from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import is_html_empty

from .fabrication import _FAB_TOKEN


_BOM_CHANGE = object()
_AUDIT = {'acme_bom_change_snapshot', 'acme_bom_change_odm_id', 'acme_bom_change_dm_id', 'acme_revised_bom_id'}


class ApprovalCategory(models.Model):
    _inherit = 'approval.category'

    acme_kind = fields.Selection(selection_add=[('bom_change', 'BoM Change')], ondelete={'bom_change': 'set null'})


class FabricationLine(models.Model):
    _inherit = 'acme.fabrication.line'

    acme_specification_source_id = fields.Many2one(
        'acme.fabrication.line', string='Use Prior Fabrication Specification',
        check_company=True, ondelete='restrict', copy=False,
    )

    def _acme_source_component_values(self):
        source = self.acme_specification_source_id
        if not source:
            return []
        return [{
            'product_id': part.product_id.id, 'quantity': part.product_qty, 'uom_id': part.product_uom_id.id,
            'acme_material_classification': source._acme_component_classification(part),
        } for part in source.bom_id.bom_line_ids.sorted('id')]

    def _acme_component_classification(self, part):
        matches = self.component_ids.filtered(
            lambda component: component.product_id == part.product_id and component.uom_id == part.product_uom_id
        )
        return matches.acme_material_classification if len(matches) == 1 else False

    @api.onchange('acme_specification_source_id')
    def _onchange_acme_specification_source(self):
        source = self.acme_specification_source_id
        if source:
            self.update({'product_id': source.product_id, 'uom_id': source.uom_id,
                         'quantity': source.quantity, 'name': source.name})
            self.component_ids = [Command.clear()] + [
                Command.create(values) for values in self._acme_source_component_values()
            ]

    def _validate_specification(self):
        super()._validate_specification()
        for line in self.filtered('acme_specification_source_id'):
            source = line.acme_specification_source_id
            source.check_access('read')
            # Use the same source-row lock as DM authorization.
            self.env.cr.execute('SELECT id FROM acme_fabrication_line WHERE id = %s FOR UPDATE', [source.id])
            source.invalidate_recordset()
            if (not source.bom_id or source.request_id == line.request_id
                    or source.request_id.project_id != line.request_id.project_id
                    or source.product_id != line.product_id or source.uom_id != line.uom_id
                    or line.uom_id.compare(source.quantity, line.quantity)):
                raise ValidationError(_('Reuse a BoM from a prior request on this project with the same product, unit and batch size.'))
            expected = sorted((part['product_id'], part['uom_id'], part['quantity'])
                              for part in line._acme_source_component_values())
            actual = sorted((part.product_id.id, part.uom_id.id, part.quantity) for part in line.component_ids)
            if actual != expected:
                raise ValidationError(_('The components differ from the latest authorized BoM. Reselect the source on a draft request and submit it again.'))

    def _acme_apply_bom_change(self, component, quantity):
        self.ensure_one()
        source = component.bom_id
        commands = []
        for part in source.bom_line_ids.sorted('id'):
            values = part.copy_data()[0]
            if part == component:
                values['product_qty'] = quantity
            commands.append(Command.create(values))
        revised = source.with_context(acme_fab_token=_FAB_TOKEN).copy({
            'acme_request_line_id': self.id, 'bom_line_ids': commands,
            'code': _('%s / Revised BoM', source.code or source.display_name),
        })
        self.with_context(acme_fab_token=_FAB_TOKEN).bom_id = revised
        return revised


class BomChangeRequest(models.Model):
    _inherit = 'approval.request'

    acme_bom_component_id = fields.Many2one('mrp.bom.line', ondelete='restrict', copy=False, check_company=True)
    acme_bom_change_current_qty = fields.Float(compute='_compute_bom_change_current_qty', string='Current Component Quantity')
    acme_bom_change_quantity = fields.Float(string='Proposed Component Quantity')
    acme_bom_change_consult_edm = fields.Boolean(string='Consult EDM')
    acme_bom_change_snapshot = fields.Json(readonly=True, copy=False)
    acme_bom_change_dm_id = fields.Many2one('res.users', readonly=True, copy=False)
    acme_bom_change_odm_id = fields.Many2one('res.users', readonly=True, copy=False)
    acme_revised_bom_id = fields.Many2one('mrp.bom', readonly=True, copy=False, ondelete='restrict')

    def _acme_requires_financial_approval(self):
        return self.acme_kind != 'bom_change' and super()._acme_requires_financial_approval()

    @api.depends('acme_bom_component_id.product_qty', 'acme_bom_change_snapshot')
    def _compute_bom_change_current_qty(self):
        for request in self:
            baseline = request.acme_bom_change_snapshot
            request.acme_bom_change_current_qty = baseline['quantity'] if baseline else request.acme_bom_component_id.product_qty

    @api.model_create_multi
    def create(self, vals_list):
        if any(_AUDIT.intersection(vals) for vals in vals_list) or any(
            self.env.context.get(f'default_{field}') for field in _AUDIT
        ):
            raise AccessError(_('BoM revision audit fields are generated by the workflow.'))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get('acme_bom_change_token') is not _BOM_CHANGE:
            if _AUDIT.intersection(vals):
                raise AccessError(_('BoM revision audit fields are generated by the workflow.'))
            locked = {'acme_bom_component_id', 'acme_bom_change_quantity', 'acme_bom_change_consult_edm',
                      'acme_project_id', 'category_id', 'request_owner_id', 'reason', 'approver_ids'}
            if locked.intersection(vals) and self.filtered('acme_bom_change_snapshot'):
                raise UserError(_('Submitted BoM changes are locked. Submit a new request to make corrections.'))
        return super().write(vals)

    def _acme_lock_bom_change(self):
        self.ensure_one()
        self.check_access('write')
        self.env.cr.execute('UPDATE approval_request SET id = id WHERE id = %s', [self.id])
        self.invalidate_recordset()

    def _acme_bom_change_source(self, update=False):
        component = self.acme_bom_component_id
        if not component:
            raise UserError(_('Select the fabrication BoM component to revise.'))
        component.check_access('read')
        bom = component.bom_id
        line = bom.acme_request_line_id
        if not line or line.request_id.project_id != self.acme_project_id:
            raise UserError(_('Select an existing fabrication BoM component from this project.'))
        line.check_access('write' if update else 'read')
        query = ('UPDATE acme_fabrication_line SET id = id WHERE id = %s' if update else
                 'SELECT id FROM acme_fabrication_line WHERE id = %s FOR UPDATE')
        self.env.cr.execute(query, [line.id])
        line.invalidate_recordset()
        component.invalidate_recordset()
        bom.invalidate_recordset()
        if bom != line.bom_id:
            raise UserError(_('The selected BoM has been superseded. Submit a new request against the current revision.'))
        return line, component, {
            'bom': bom.id, 'component': component.id, 'quantity': component.product_qty,
            'product': component.product_id.id, 'uom': component.product_uom_id.id,
            'component_updated': str(component.write_date), 'bom_updated': str(bom.write_date),
        }

    def _acme_bom_position_user(self, position):
        user = self.env['acme.approval.position.assignment'].for_position(self.company_id, position).user_id
        if not user.active or self.company_id not in user.company_ids or not user.has_group(
            f'acme_project_core.group_acme_{position}'
        ):
            raise UserError(_('Configure an active %(role)s with access to this company.', role=position.upper()))
        return user

    def action_confirm(self):
        self.ensure_one()
        if self.acme_kind != 'bom_change':
            return super().action_confirm()
        self._acme_lock_bom_change()
        project = self.acme_project_id
        project.check_access('read')
        if not project or self.request_owner_id != self.env.user or project.user_id != self.env.user:
            raise AccessError(_('Only the assigned PM can submit a project BoM Change.'))
        if self.acme_bom_change_snapshot or self.request_status != 'new':
            raise UserError(_('Only a new BoM Change can be submitted.'))
        if self.category_id.approval_minimum != 2 or not self.category_id.approver_sequence or self.category_id.manager_approval:
            raise UserError(_('Configure BoM Change with two sequential approvals and no automatic manager approval.'))
        _line, component, baseline = self._acme_bom_change_source()
        quantity = self.acme_bom_change_quantity
        if (not math.isfinite(quantity) or quantity <= 0
                or component.product_uom_id.compare(quantity, baseline['quantity']) == 0 or is_html_empty(self.reason)):
            raise UserError(_('Enter a different positive component quantity and a reason.'))
        dm = self._acme_bom_position_user('dm')
        odm = self._acme_bom_position_user('odm')
        if len(dm | odm | self.env.user) != 3:
            raise UserError(_('Configure different PM, ODM and DM users.'))
        edm = self._acme_bom_position_user('edm') if self.acme_bom_change_consult_edm else self.env['res.users']
        request = self.with_context(acme_bom_change_token=_BOM_CHANGE)
        request.write({
            'acme_bom_change_snapshot': baseline, 'acme_bom_change_dm_id': dm.id,
            'acme_bom_change_odm_id': odm.id,
            'approver_ids': [Command.clear(),
                             Command.create({'user_id': odm.id, 'required': True, 'sequence': 10}),
                             Command.create({'user_id': dm.id, 'required': True, 'sequence': 20})],
        })
        result = super(BomChangeRequest, request).action_confirm()
        if edm:
            project.activity_schedule(
                'mail.mail_activity_data_todo', user_id=edm.id,
                summary=_('Consult on BoM Change: %s', self.display_name),
                note=_('Review %(product)s: %(before)s → %(after)s %(unit)s.',
                       product=component.product_id.display_name, before=baseline['quantity'],
                       after=quantity, unit=component.product_uom_id.name),
            )
        return result

    def action_approve(self, approver=None):
        if not self.filtered(lambda request: request.acme_kind == 'bom_change'):
            return super().action_approve(approver=approver)
        self._acme_lock_bom_change()
        if self.acme_revised_bom_id:
            return
        odm = self.acme_bom_change_odm_id
        if not odm:
            raise UserError(_('This request predates sequential BoM approvals. Submit a new request.'))
        dm = self.acme_bom_change_dm_id
        actor = self.approver_ids.filtered(lambda row: row.user_id == self.env.user)
        if self.request_status != 'pending' or actor.status != 'pending':
            raise UserError(_('This BoM Change is no longer awaiting authorization.'))
        request = self.with_context(acme_bom_change_token=_BOM_CHANGE)
        if self.env.user == odm and self.env.user.has_group('acme_project_core.group_acme_odm'):
            return super(BomChangeRequest, request).action_approve()
        if self.env.user != dm or not self.env.user.has_group('acme_project_core.group_acme_dm') or not self.approver_ids.filtered(
            lambda row: row.user_id == odm and row.status == 'approved'
        ):
            raise AccessError(_('Only the designated DM may authorize after the ODM has approved.'))
        line, component, baseline = self._acme_bom_change_source(update=True)
        if baseline != self.acme_bom_change_snapshot:
            raise UserError(_('The source component changed after submission. Submit a fresh BoM Change.'))
        result = super(BomChangeRequest, request).action_approve()
        if request.request_status != 'approved':
            raise UserError(_('The BoM Change did not reach final approval.'))
        request.acme_revised_bom_id = line._acme_apply_bom_change(component, self.acme_bom_change_quantity)
        return result

    def _action_force_approval(self):
        if self.filtered(lambda request: request.acme_kind == 'bom_change'):
            raise UserError(_('BoM Change requires authorization by the designated DM.'))
        return super()._action_force_approval()


class BomChangeApprover(models.Model):
    _inherit = 'approval.approver'

    def _acme_check_bom_change_route(self):
        if self.env.context.get('acme_bom_change_token') is not _BOM_CHANGE and self.request_id.filtered('acme_bom_change_snapshot'):
            raise AccessError(_('BoM Change approval routing is locked.'))

    @api.model_create_multi
    def create(self, vals_list):
        approvers = super().create(vals_list)
        approvers._acme_check_bom_change_route()
        return approvers

    def write(self, vals):
        if {'request_id', 'user_id', 'sequence', 'required'}.intersection(vals) or vals.get('status') == 'approved':
            self._acme_check_bom_change_route()
            if vals.get('request_id') and self.env['approval.request'].browse(vals['request_id']).acme_bom_change_snapshot:
                raise AccessError(_('BoM Change approval routing is locked.'))
        if 'status' in vals and self.request_id.filtered('acme_revised_bom_id'):
            raise UserError(_('An applied BoM Change decision is historical. Submit a new change to revise it.'))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_bom_change_route(self):
        self._acme_check_bom_change_route()
