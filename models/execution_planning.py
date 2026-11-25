from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.acme_project_core.models.boq_revision import snapshot
from odoo.addons.acme_project_approvals.models.preparation import _PREPARATION_TOKEN


_PLAN_TOKEN = object()
_TASK_PLAN_ORIGIN = object()
_PLAN_FIELDS = {
    'acme_is_work_package', 'acme_execution_boq_id', 'acme_execution_bom_id',
    'name', 'description', 'parent_id', 'child_ids', 'project_id', 'company_id', 'active',
    'sequence', 'user_ids', 'acme_execution_approach', 'acme_execution_skill_ids',
    'acme_execution_role_id', 'acme_start_date', 'acme_end_date', 'acme_uom_id',
    'acme_planned_quantity', 'planned_date_begin', 'date_deadline',
}
_AUDIT_FIELDS = {'acme_execution_signoff_id', 'acme_execution_plan_snapshot', 'acme_execution_started'}


class ExecutionWorkPackage(models.Model):
    _inherit = 'project.task'

    acme_is_work_package = fields.Boolean(string='BOQ Execution Work Package', copy=False)
    acme_execution_boq_id = fields.Many2one(
        'acme.project.boq', string='Contract BOQ Line', check_company=True, ondelete='restrict', copy=False,
    )
    acme_execution_bom_id = fields.Many2one(
        'mrp.bom', string='Approved Execution BoM', check_company=True, ondelete='restrict', copy=False,
    )
    acme_eligible_execution_bom_ids = fields.Many2many('mrp.bom', compute='_compute_acme_eligible_execution_boms')
    acme_execution_package_id = fields.Many2one(
        'project.task', compute='_compute_acme_execution_package', recursive=True, store=True,
    )
    acme_package_boq_id = fields.Many2one(related='acme_execution_package_id.acme_execution_boq_id', store=True)
    acme_package_bom_id = fields.Many2one(related='acme_execution_package_id.acme_execution_bom_id')
    acme_execution_order_id = fields.Many2one(related='project_id.acme_order_id')
    acme_execution_signoff_id = fields.Many2one('approval.request', string='Approved Task Plan Revision', readonly=True, copy=False, ondelete='restrict')
    acme_execution_signoff_status = fields.Selection(related='acme_execution_signoff_id.request_status')
    acme_execution_plan_snapshot = fields.Json(readonly=True, copy=False)
    acme_execution_started = fields.Boolean(readonly=True, copy=False)
    acme_execution_measurement_ids = fields.One2many('acme.project.measurement', 'acme_execution_task_id')

    @api.depends('acme_is_work_package', 'parent_id.acme_execution_package_id')
    def _compute_acme_execution_package(self):
        for task in self:
            task.acme_execution_package_id = task if task.acme_is_work_package else task.parent_id.acme_execution_package_id

    @api.depends('project_id', 'project_id.company_id')
    def _compute_acme_eligible_execution_boms(self):
        Boms = self.env['mrp.bom']
        for task in self:
            task.acme_eligible_execution_bom_ids = Boms
            if task.project_id.company_id and Boms.has_access('read'):
                candidates = Boms.search([
                    ('company_id', '=', task.project_id.company_id.id),
                    ('acme_request_line_id.request_id.project_id', '=', task.project_id.id),
                    ('acme_request_line_id.request_id.state', 'in', ['received', 'reported', 'accepted']),
                ])
                task.acme_eligible_execution_bom_ids = candidates.filtered(
                    lambda bom: bom.acme_request_line_id.bom_id == bom
                )

    def _acme_check_execution_mapping(self, current_bom=False):
        self.ensure_one()
        project, line, bom = self.project_id, self.acme_execution_boq_id, self.acme_execution_bom_id
        if not self.acme_is_work_package and (line or bom):
            raise ValidationError(_('Enable BOQ Execution Work Package before setting its references.'))
        if self.acme_is_work_package and (not project.company_id or self.parent_id.acme_execution_package_id):
            raise ValidationError(_('Use a work package on a project with a company, outside any other work package.'))
        if self.acme_execution_package_id and self.acme_execution_package_id.project_id != project:
            raise ValidationError(_('Keep work-package subtasks in the same project.'))
        if line:
            line.check_access('read')
            source = line.source_line_id
            order = source.order_id
            if (line.project_id != project or line.company_id != project.company_id
                    or not project.acme_boq_initialized or not order or order != project.acme_order_id
                    or order.state != 'sale' or not getattr(order, 'x_tender_id', False)):
                raise ValidationError(_('Select an imported BOQ line from this project’s confirmed Tender contract.'))
        if bom:
            bom.check_access('read')
            origin = bom.acme_request_line_id
            if (bom.company_id != project.company_id or origin.request_id.project_id != project
                    or origin.request_id.state not in ('received', 'reported', 'accepted')
                    or not bom.active or (current_bom and origin.bom_id != bom)):
                raise ValidationError(_('Select a current approved fabrication BoM for this project and company.'))

    @api.constrains('acme_execution_boq_id', 'acme_execution_bom_id', 'acme_is_work_package', 'parent_id', 'project_id', 'company_id')
    def _check_acme_execution_mapping(self):
        for task in self:
            task._acme_check_execution_mapping()

    def _acme_plan_steps(self):
        self.ensure_one()
        result = self
        for child in self.child_ids.sorted(lambda item: (item.sequence, item.id)):
            result |= child._acme_plan_steps()
        return result

    def _acme_execution_plan(self):
        self.ensure_one()
        self._acme_check_execution_mapping(current_bom=True)
        project, line, bom = self.project_id, self.acme_execution_boq_id, self.acme_execution_bom_id
        if not (line and bom and self.acme_execution_approach
                and self.acme_uom_id and self.acme_planned_quantity > 0 and self.child_ids):
            raise UserError(_('Complete the BOQ, approved BoM, execution approach, planned quantity, unit, and subtasks for %s.', self.display_name))
        tasks = self._acme_plan_steps()
        if any(task.project_id != project or task.company_id != project.company_id or
               not task.acme_start_date or not task.acme_end_date or not task.user_ids or
               any(not user.active or user.share for user in task.user_ids) for task in tasks):
            raise UserError(_('Set planned dates and active assignees on the work package and all its subtasks.'))
        revisions = project.acme_boq_history_ids.filtered(
            lambda rev: rev.state == 'approved' and any(
                change.get('line_id') == line.id for change in (rev.changes or [])
            )
        )
        return {
            'boq': [line.id, line.source_line_id.id, snapshot(line), line.selling_rate, sorted(revisions.ids)],
            'bom': bom.id,
            'tasks': [[task.id, task.parent_id.id, task.sequence, task.name, str(task.description or ''),
                       task.acme_uom_id.id, task.acme_planned_quantity, sorted(task.user_ids.ids),
                       str(task.acme_start_date), str(task.acme_end_date),
                       str(getattr(task, 'planned_date_begin', False) or ''), str(task.date_deadline or ''),
                       task.acme_execution_approach or False, task.acme_execution_role_id.id,
                       sorted(task.acme_execution_skill_ids.ids)] for task in tasks],
        }

    def _acme_require_execution_signoff(self):
        for task in self:
            if (task.acme_execution_signoff_status != 'approved'
                    or task.acme_execution_plan_snapshot != task._acme_execution_plan()):
                raise UserError(_('Submit the current work package through the project Task Plan Approval before execution.'))

    def _acme_invalidate_execution_plan(self):
        packages = self.filtered(lambda task: task.acme_execution_plan_snapshot or task.acme_execution_started)
        if packages:
            packages.with_context(acme_execution_plan_token=_PLAN_TOKEN).write({
                'acme_execution_plan_snapshot': False, 'acme_execution_started': False,
            })

    @api.model_create_multi
    def create(self, vals_list):
        if any(_AUDIT_FIELDS.intersection(vals) for vals in vals_list) or any(
            self.env.context.get(f'default_{name}') for name in _AUDIT_FIELDS
        ):
            raise AccessError(_('Approve and start work packages through their workflow actions.'))
        tasks = super().create(vals_list)
        for task in tasks.filtered('acme_execution_bom_id'):
            task._acme_check_execution_mapping(current_bom=True)
        packages = tasks.acme_execution_package_id
        if packages:
            packages.project_id._acme_check_preparation_editor()
            packages.project_id._acme_lock_task_plans()
            packages._acme_invalidate_execution_plan()
            tasks.filtered(lambda task: task.acme_executed_quantity or task.state in ('03_approved', '1_done')
                           ).acme_execution_package_id._acme_require_execution_signoff()
        return tasks

    def write(self, vals):
        if _AUDIT_FIELDS.intersection(vals) and self.env.context.get('acme_execution_plan_token') is not _PLAN_TOKEN:
            raise AccessError(_('Approve and start work packages through their workflow actions.'))
        plan_changes = _PLAN_FIELDS.intersection(vals)
        packages = self.acme_execution_package_id if plan_changes else self.browse()
        if plan_changes:
            new_parent = self.browse(vals.get('parent_id') or [])
            packages |= new_parent.acme_execution_package_id
            if packages or vals.get('acme_is_work_package'):
                projects = packages.project_id | self.project_id | self.env['project.project'].browse(vals.get('project_id') or [])
                projects._acme_check_preparation_editor()
                projects._acme_lock_task_plans()
            if 'acme_is_work_package' in vals and not vals['acme_is_work_package'] and self.filtered('acme_execution_signoff_id'):
                raise UserError(_('Keep approved work packages for planning history.'))
            if packages and {'acme_execution_boq_id', 'project_id', 'parent_id'}.intersection(vals):
                measured = self.env['acme.project.measurement'].search_count([
                    ('acme_execution_task_id', 'child_of', self.ids),
                ], limit=1)
                if measured:
                    raise UserError(_('Keep the BOQ and parent mapping for tasks with linked measurements.'))
        result = super().write(vals)
        if 'acme_execution_bom_id' in vals:
            for task in self.filtered('acme_execution_bom_id'):
                task._acme_check_execution_mapping(current_bom=True)
        if plan_changes:
            (packages | self.acme_execution_package_id)._acme_invalidate_execution_plan()
        if vals.get('acme_executed_quantity') or vals.get('state') in ('01_in_progress', '03_approved', '1_done'):
            self.acme_execution_package_id._acme_require_execution_signoff()
        return result

    def unlink(self):
        if self.filtered('acme_execution_signoff_id'):
            raise UserError(_('Archive previously approved execution work packages to retain planning history.'))
        packages = self.acme_execution_package_id - self
        if packages:
            packages.project_id._acme_check_preparation_editor()
            packages.project_id._acme_lock_task_plans()
            packages._acme_invalidate_execution_plan()
        return super().unlink()

    def action_acme_start_execution(self):
        self.ensure_one()
        self.check_access('write')
        if not self.acme_is_work_package:
            raise UserError(_('Start execution on the parent work package.'))
        self.project_id._acme_check_preparation_editor()
        self._acme_require_execution_signoff()
        self.with_context(acme_execution_plan_token=_PLAN_TOKEN).acme_execution_started = True

    def action_acme_open_signed_measurements(self):
        self.ensure_one()
        self.check_access('read')
        tasks = self._acme_plan_steps() if self.acme_is_work_package else self
        measurements = self.env['acme.project.measurement'].search([
            ('acme_execution_task_id', 'in', tasks.ids), ('acme_billing_basis', '=', 'measured'),
        ])
        measurements = measurements.with_context(bin_size=True).filtered('certificate_id.signed_takeoff')
        return {
            'type': 'ir.actions.act_window', 'name': _('Signed Takeoff Measurements'),
            'res_model': 'acme.project.measurement', 'view_mode': 'list',
            'views': [(self.env.ref('acme_workshop_fabrication.signed_execution_measurement_list').id, 'list')],
            'domain': [('id', 'in', measurements.ids)],
        }


class ProjectExecutionPreparation(models.Model):
    _inherit = 'project.project'

    acme_task_plan_request_id = fields.Many2one(
        'approval.request', string='Task Plan Approval', compute='_compute_acme_task_plan_request',
        readonly=True,
    )
    acme_task_plan_status = fields.Selection(
        related='acme_task_plan_request_id.request_status', string='Task Plan Status', readonly=True,
    )

    @api.depends('task_ids.acme_execution_signoff_id', 'task_ids.acme_execution_plan_snapshot')
    def _compute_acme_task_plan_request(self):
        Request = self.env['approval.request']
        requests = Request.search([
            ('acme_kind', '=', 'task_plan'), ('acme_project_id', 'in', self.ids),
        ], order='id desc')
        by_project = {}
        for request in requests:
            by_project.setdefault(request.acme_project_id.id, request)
        for project in self:
            project.acme_task_plan_request_id = by_project.get(project.id)

    def _acme_lock_task_plans(self):
        self.check_access('write')
        for project in self.sorted('id'):
            self.env.cr.execute('UPDATE project_project SET id = id WHERE id = %s', [project.id])
        self.env.invalidate_all()

    def _acme_pending_task_plan(self):
        self.ensure_one()
        return self.env['approval.request'].search([
            ('acme_kind', '=', 'task_plan'), ('acme_project_id', '=', self.id),
            ('request_status', '=', 'pending'),
        ], limit=1)

    def action_acme_submit_task_plan(self):
        self.ensure_one()
        self._acme_lock_task_plans()
        if self.user_id != self.env.user:
            raise AccessError(_('Only the assigned PM can submit the Task Plan.'))
        request = self._acme_pending_task_plan()
        if not request:
            categories = self.env['approval.category'].search([
                ('acme_kind', '=', 'task_plan'), ('company_id', '=', self.company_id.id), ('active', '=', True),
            ])
            if len(categories) != 1:
                raise UserError(_('Configure exactly one active Task Plan Approval type for this company.'))
            request = self.env['approval.request'].with_context(acme_task_plan_origin=_TASK_PLAN_ORIGIN).create({
                'name': _('Task Plan Approval: %s', self.display_name),
                'category_id': categories.id, 'acme_project_id': self.id, 'request_owner_id': self.env.uid,
            })
            request.name = _('Task Plan Approval #%(revision)s: %(project)s', revision=request.id, project=self.display_name)
            request.action_confirm()
        return {'type': 'ir.actions.act_window', 'res_model': 'approval.request',
                'view_mode': 'form', 'res_id': request.id}

    def _acme_task_plan_snapshot(self):
        self.ensure_one()
        submitted = []
        for package in self.task_ids.filtered(lambda task: task.active and task.acme_is_work_package).sorted('id'):
            plan = package._acme_execution_plan()
            if package.acme_execution_signoff_status == 'approved' and package.acme_execution_plan_snapshot == plan:
                continue
            submitted.append({
                'package': package.id, 'plan': plan,
                'boq_label': package.acme_execution_boq_id.display_name,
                'bom_label': package.acme_execution_bom_id.display_name,
                'steps': [[task.id, task.parent_id.id, task.name, task.acme_planned_quantity,
                           task.acme_uom_id.display_name or '', str(task.acme_start_date), str(task.acme_end_date)]
                          for task in package._acme_plan_steps()],
            })
        if not submitted:
            raise UserError(_('There are no new or changed BOQ work packages to submit.'))
        return submitted


class ExecutionPlanCategory(models.Model):
    _inherit = 'approval.category'

    acme_kind = fields.Selection(selection_add=[('task_plan', 'Task Plan Approval')],
                                 ondelete={'task_plan': 'set null'})


class ExecutionPreparationApproval(models.Model):
    _inherit = 'approval.request'

    acme_task_plan_snapshot = fields.Json(readonly=True, copy=False)
    acme_task_plan_details = fields.Html(compute='_compute_task_plan_details')

    @api.depends('acme_task_plan_snapshot')
    def _compute_task_plan_details(self):
        for request in self:
            sections = []
            for entry in request.acme_task_plan_snapshot or []:
                depth = {}
                rows = []
                for task_id, parent_id, name, quantity, unit, start, end in entry['steps']:
                    depth[task_id] = depth.get(parent_id, -1) + 1
                    label = Markup('&nbsp;' * (4 * depth[task_id])) + name
                    rows.append(Markup('<tr><td>%s</td><td>%s %s</td><td>%s</td><td>%s</td></tr>') %
                                (label, quantity, unit, start, end))
                headers = (_('Task'), _('Planned Quantity'), _('Start Date'), _('End Date'))
                sections.append(Markup(
                    '<p><strong>BOQ:</strong> %s <strong>BoM:</strong> %s</p>'
                    '<table class="table table-sm"><thead><tr>%s</tr></thead><tbody>%s</tbody></table>'
                ) % (entry['boq_label'], entry['bom_label'],
                     Markup('').join(Markup('<th>%s</th>') % header for header in headers),
                     Markup('').join(rows)))
            request.acme_task_plan_details = Markup('').join(sections)

    def _acme_requires_financial_approval(self):
        return self.acme_kind != 'task_plan' and super()._acme_requires_financial_approval()

    @api.model_create_multi
    def create(self, vals_list):
        if any('acme_task_plan_snapshot' in vals for vals in vals_list) or 'default_acme_task_plan_snapshot' in self.env.context:
            raise AccessError(_('The task plan snapshot is recorded through project submission.'))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get('acme_preparation_token') is not _PREPARATION_TOKEN and (
            'acme_task_plan_snapshot' in vals or vals.get('request_status') == 'approved'
            and self.filtered(lambda request: request.acme_kind == 'task_plan')
        ):
            raise AccessError(_('Use Task Plan Approval to record planning decisions.'))
        return super().write(vals)

    def _acme_preparation_kinds(self):
        return (*super()._acme_preparation_kinds(), 'task_plan')

    def _acme_extra_preparation_submission(self):
        if self.acme_kind != 'task_plan':
            return super()._acme_extra_preparation_submission()
        if self.env.context.get('acme_task_plan_origin') is not _TASK_PLAN_ORIGIN or self.acme_project_id.user_id != self.env.user:
            raise AccessError(_('Submit the task plan from the project as its assigned PM.'))
        return {'acme_task_plan_snapshot': self.acme_project_id._acme_task_plan_snapshot()}, self._acme_preparation_actors(('odm', 'dm'))

    def _acme_validate_preparation_decision(self):
        super()._acme_validate_preparation_decision()
        if self.acme_kind != 'task_plan':
            return
        role = 'odm' if self.env.uid == self.acme_preparation_route[0] else 'dm'
        if not self.env.user.has_group('acme_project_core.group_acme_' + role):
            raise AccessError(_('Only the designated %(role)s may sign the Task Plan.', role=role.upper()))
        self.acme_project_id._acme_lock_task_plans()
        for entry in self.acme_task_plan_snapshot:
            package = self.env['project.task'].browse(entry['package']).exists()
            if (not package or not package.active or not package.acme_is_work_package
                    or package.project_id != self.acme_project_id or package._acme_execution_plan() != entry['plan']):
                raise UserError(_('The submitted task plan changed. Cancel this request and submit the current plan.'))

    def _acme_complete_preparation(self):
        super()._acme_complete_preparation()
        if self.acme_kind != 'task_plan':
            return
        Tasks = self.env['project.task'].with_context(acme_execution_plan_token=_PLAN_TOKEN)
        for entry in self.acme_task_plan_snapshot:
            Tasks.browse([row[0] for row in entry['plan']['tasks']]).write({'acme_execution_signoff_id': self.id})
            Tasks.browse(entry['package']).acme_execution_plan_snapshot = entry['plan']


class ExecutionMeasurement(models.Model):
    _inherit = 'acme.project.measurement'

    acme_execution_task_id = fields.Many2one(
        'project.task', string='Originating Execution Task', check_company=True, ondelete='restrict', copy=False,
    )

    @api.constrains('acme_execution_task_id', 'boq_id', 'certificate_id')
    def _check_acme_execution_task(self):
        for measurement in self.filtered('acme_execution_task_id'):
            task = measurement.acme_execution_task_id
            task.check_access('read')
            if (not task.acme_execution_package_id or task.project_id != measurement.project_id
                    or task.company_id != measurement.company_id or task.acme_package_boq_id != measurement.boq_id):
                raise ValidationError(_('Select an execution task for the same project, company, and BOQ line.'))
