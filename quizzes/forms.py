"""Forms and inline formsets for instructor quiz authoring.

A quiz is edited on one page: the quiz settings plus a question formset, and
inside every question a choice formset.  The two levels are plain Django
inline formsets (no JavaScript); the choice formset enforces the rules the
brief requires (2-6 choices, exactly one correct, no blank options).
"""

from django import forms
from django.db import models
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils.translation import gettext_lazy as _

from .models import Choice, Question, Quiz

#: Number of choice rows rendered per question (blank rows are ignored).
CHOICE_SLOTS = 6
MIN_CHOICES = 2
MAX_CHOICES = 6


class QuizForm(forms.ModelForm):
    """Quiz settings.  ``max_attempts`` is optional (blank means unlimited)."""

    class Meta:
        model = Quiz
        fields = ["pass_mark", "max_attempts", "show_answers"]


class QuestionForm(forms.ModelForm):
    """A single question; its position is assigned when it is first saved."""

    class Meta:
        model = Question
        fields = ["text"]
        widgets = {"text": forms.Textarea(attrs={"rows": 2})}

    def save(self, commit: bool = True) -> Question:
        question = super().save(commit=False)
        # ``position`` is required by the model but not part of the form: the
        # inline formset has just set ``question.quiz``, so the next free
        # position is known here.  ``resequence_questions`` compacts the
        # numbers afterwards.
        if question.position is None:
            highest = question.quiz.questions.aggregate(highest=models.Max("position"))[
                "highest"
            ]
            question.position = (highest or 0) + 1
        if commit:
            question.save()
        return question


class ChoiceForm(forms.ModelForm):
    """One answer option; blank rows in the fixed slots are simply ignored."""

    class Meta:
        model = Choice
        fields = ["text", "is_correct"]


class BaseChoiceFormSet(BaseInlineFormSet):
    """Validate the "2 to 6 choices, exactly one correct" rule per question."""

    def clean(self) -> None:
        super().clean()
        # If any individual choice is invalid, its own error is clearer than a
        # question-level count error, so stop here.
        if any(self.errors):
            return
        choices = [
            form
            for form in self.forms
            if form.cleaned_data.get("text")
            and not (self.can_delete and self._should_delete_form(form))
        ]
        if not MIN_CHOICES <= len(choices) <= MAX_CHOICES:
            raise forms.ValidationError(
                _("A question must have between %(min)d and %(max)d choices.")
                % {"min": MIN_CHOICES, "max": MAX_CHOICES}
            )
        correct = [form for form in choices if form.cleaned_data.get("is_correct")]
        if len(correct) != 1:
            raise forms.ValidationError(_("Mark exactly one choice as correct."))


QuestionFormSet = inlineformset_factory(
    Quiz,
    Question,
    form=QuestionForm,
    extra=1,
    can_delete=False,
)

ChoiceFormSet = inlineformset_factory(
    Question,
    Choice,
    form=ChoiceForm,
    formset=BaseChoiceFormSet,
    extra=0,
    can_delete=True,
)
